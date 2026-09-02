"""Gateway-first permission decorator and unique raw execution middleware."""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import AsyncGenerator, Callable
from contextvars import ContextVar, Token
from dataclasses import dataclass
import asyncio
import json
import time
from typing import Any, Mapping
import uuid

from agentscope.message import SystemMsg, TextBlock, ToolCallBlock, ToolResultState
from agentscope.middleware import MiddlewareBase
from agentscope.model import ChatResponse
from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.tool import ToolChoice, ToolChunk, ToolResponse

from qwenpaw.hooks.base import LifecycleHook
from qwenpaw.runtime.hooks import HookContext, HookResult
from qwenpaw.runtime.phases import Phase

from .client import GatewayClient, GatewayConfig, GatewayError
from .protocol import canonical_payload_hash
from ..artifact_tools import (
    ARTIFACT_WORKER_TOOL_NAMES,
    ArtifactDeliveryIntent,
    ArtifactToolNotInvokedError,
    artifact_delivery_intent_from_request,
)
from ..sandbox.executor import RuntimeSandboxExecutor, is_physical_tool

_BLOCKED_NESTED_TOOLS = frozenset({"run_tool_batch"})
_SANDBOX_BROKER_TOOLS = frozenset(
    {"runtime_sandbox_files_search", "runtime_sandbox_files_select"}
)
_RUNTIME_EXECUTED_TOOLS = ARTIFACT_WORKER_TOOL_NAMES


@dataclass
class _ArtifactTurnState:
    intent: ArtifactDeliveryIntent
    replan_count: int = 0
    invoked: bool = False
    failed: bool = False


@dataclass(frozen=True)
class _CapturedModelOutput:
    chunks: tuple[ChatResponse, ...]
    streamed: bool

    @property
    def final(self) -> ChatResponse:
        for chunk in reversed(self.chunks):
            if chunk.is_last:
                return chunk
        return self.chunks[-1]

    def replay(self) -> ChatResponse | AsyncGenerator[ChatResponse, None]:
        if not self.streamed:
            return self.final

        async def generate() -> AsyncGenerator[ChatResponse, None]:
            for chunk in self.chunks:
                yield chunk

        return generate()


_artifact_turn_state: ContextVar[_ArtifactTurnState | None] = ContextVar(
    "bank_runtime_artifact_turn_state",
    default=None,
)


@dataclass
class _PreparedExecution:
    tool_name: str
    input_hash: str
    preflight: dict[str, Any]
    claimed: bool = False


class BankRuntimeGatewayMiddleware(MiddlewareBase):
    """Execute only calls already admitted by Runtime and Tool Guard."""

    def __init__(
        self,
        client: Any | None,
        *,
        sandbox_executor: Any | None = None,
        configuration_error: str = "",
        artifact_intent: ArtifactDeliveryIntent | None = None,
    ) -> None:
        self.client = client
        self.sandbox_executor = sandbox_executor
        self.configuration_error = str(configuration_error or "")
        self.artifact_intent = artifact_intent
        self._prepared: dict[tuple[str, str], deque[_PreparedExecution]] = defaultdict(
            deque
        )
        self._prepared_broker: dict[tuple[str, str], int] = defaultdict(int)

    async def on_reply(
        self,
        agent: Any,
        input_kwargs: dict[str, Any],
        next_handler: Callable[..., AsyncGenerator[Any, None]],
    ) -> AsyncGenerator[Any, None]:
        if self.artifact_intent is None:
            async for item in next_handler(**input_kwargs):
                yield item
            return
        token: Token = _artifact_turn_state.set(
            _ArtifactTurnState(intent=self.artifact_intent),
        )
        try:
            async for item in next_handler(**input_kwargs):
                yield item
        finally:
            _artifact_turn_state.reset(token)

    async def on_model_call(
        self,
        agent: Any,
        input_kwargs: dict[str, Any],
        next_handler: Callable[..., Any],
    ) -> Any:
        del agent
        state = _artifact_turn_state.get()
        if state is None or state.invoked:
            return await next_handler(**input_kwargs)
        # AgentScope retries failed model middleware calls. Once this boundary
        # has failed, reject retries without issuing additional model calls.
        if state.failed:
            raise ArtifactToolNotInvokedError()

        response = await _capture_model_output(await next_handler(**input_kwargs))
        tool_names = _tool_call_names(response.final)
        if tool_names & ARTIFACT_WORKER_TOOL_NAMES:
            state.invoked = True
            return response.replay()
        # A non-artifact tool can legitimately prepare source material. The
        # next model round remains responsible for the required artifact call.
        if tool_names:
            return response.replay()
        if state.replan_count:
            state.failed = True
            raise ArtifactToolNotInvokedError()

        visible_tools = _visible_artifact_tool_schemas(input_kwargs.get("tools"))
        if not visible_tools:
            state.failed = True
            raise ArtifactToolNotInvokedError()
        state.replan_count = 1
        retry = dict(input_kwargs)
        retry["tools"] = visible_tools
        allowed_names = [
            str(schema.get("function", {}).get("name") or "")
            for schema in visible_tools
        ]
        retry["tool_choice"] = ToolChoice(mode="required", tools=allowed_names)
        retry["messages"] = [
            *list(input_kwargs.get("messages") or []),
            SystemMsg(
                name="system",
                content=(
                    "This request requires an actual governed artifact delivery. "
                    "Call exactly one available artifact tool now. Do not claim a "
                    "file was created in text and do not emit scripts or tutorials."
                ),
            ),
        ]
        replacement = await _capture_model_output(await next_handler(**retry))
        replacement_names = _tool_call_names(replacement.final)
        if replacement_names & ARTIFACT_WORKER_TOOL_NAMES:
            state.invoked = True
            return replacement.replay()
        state.failed = True
        raise ArtifactToolNotInvokedError()

    def prepare(
        self,
        tool_name: str,
        tool_input: Mapping[str, Any],
        preflight: dict[str, Any],
    ) -> None:
        key = (str(tool_name), canonical_payload_hash(tool_input))
        self._prepared[key].append(_PreparedExecution(key[0], key[1], dict(preflight)))

    def claim(
        self,
        tool_name: str,
        tool_input: Mapping[str, Any],
    ) -> _PreparedExecution:
        key = (str(tool_name), canonical_payload_hash(tool_input))
        queue = self._prepared.get(key)
        if not queue:
            raise GatewayError("Tool execution has no admitted Gateway permit")
        prepared = queue.popleft()
        if not queue:
            self._prepared.pop(key, None)
        if prepared.claimed:
            raise GatewayError("Tool Gateway permit was already claimed")
        prepared.claimed = True
        return prepared

    def prepare_broker(
        self,
        tool_name: str,
        tool_input: Mapping[str, Any],
    ) -> None:
        key = (str(tool_name), canonical_payload_hash(tool_input))
        self._prepared_broker[key] += 1

    def claim_broker(
        self,
        tool_name: str,
        tool_input: Mapping[str, Any],
    ) -> None:
        key = (str(tool_name), canonical_payload_hash(tool_input))
        remaining = self._prepared_broker.get(key, 0)
        if remaining < 1:
            raise GatewayError(
                "Sandbox broker execution has no trusted permission claim"
            )
        if remaining == 1:
            self._prepared_broker.pop(key, None)
        else:
            self._prepared_broker[key] = remaining - 1

    async def on_acting(
        self,
        agent: Any,
        input_kwargs: dict[str, Any],
        next_handler: Callable[..., AsyncGenerator[Any, None]],
    ) -> AsyncGenerator[Any, None]:
        del agent
        if self.configuration_error or self.client is None:
            raise GatewayError(
                self.configuration_error or "Runtime Tool Gateway is unavailable"
            )
        tool_call = input_kwargs.get("tool_call")
        tool_name = str(getattr(tool_call, "name", "") or "")
        try:
            tool_input = json.loads(str(getattr(tool_call, "input", "") or "{}"))
        except (TypeError, ValueError) as exc:
            raise GatewayError("Tool input is not valid JSON") from exc
        if not isinstance(tool_input, dict):
            raise GatewayError("Tool input must be an object")
        if tool_name in _SANDBOX_BROKER_TOOLS:
            self.claim_broker(tool_name, tool_input)
            async for item in next_handler():
                yield item
            return
        prepared = self.claim(tool_name, tool_input)
        try:
            await self.client.report_guard(prepared.preflight, "allow")
        except Exception as exc:
            raise GatewayError("Runtime Tool Guard acknowledgement failed") from exc
        tool_call_id = str(prepared.preflight.get("tool_call_id") or "")
        started_at = time.monotonic()
        result_reported = False
        try:
            if tool_name in _RUNTIME_EXECUTED_TOOLS:
                result = await self.client.execute_runtime_tool(
                    prepared.preflight,
                    tool_name,
                    tool_input,
                )
                yield _runtime_tool_response(
                    str(getattr(tool_call, "id", "") or tool_call_id),
                    result,
                )
                return
            if is_physical_tool(tool_name):
                if self.sandbox_executor is None:
                    raise GatewayError("Runtime physical sandbox is unavailable")
                result = await self.sandbox_executor.execute(
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    tool_input=tool_input,
                )
                response = _sandbox_tool_response(
                    str(getattr(tool_call, "id", "") or tool_call_id),
                    tool_name,
                    result,
                )
                status, error_code = _result_status(response)
                await self.client.report_result(
                    tool_call_id,
                    status,
                    _duration_ms(started_at),
                    error_code,
                )
                result_reported = True
                yield response
                return
            async for item in next_handler():
                if isinstance(item, ToolResponse) and not result_reported:
                    status, error_code = _result_status(item)
                    await self.client.report_result(
                        tool_call_id,
                        status,
                        _duration_ms(started_at),
                        error_code,
                    )
                    result_reported = True
                elif (
                    isinstance(item, ToolChunk)
                    and not result_reported
                    and item.state
                    in {
                        ToolResultState.ERROR,
                        ToolResultState.INTERRUPTED,
                        ToolResultState.DENIED,
                    }
                ):
                    status, error_code = _result_status(item)
                    await self.client.report_result(
                        tool_call_id,
                        status,
                        _duration_ms(started_at),
                        error_code,
                    )
                    result_reported = True
                yield item
        except asyncio.CancelledError:
            if not result_reported:
                await self.client.report_result(
                    tool_call_id,
                    "cancelled",
                    _duration_ms(started_at),
                    "TOOL_EXECUTION_CANCELLED",
                )
            raise
        except Exception:
            if not result_reported:
                await self.client.report_result(
                    tool_call_id,
                    "failed",
                    _duration_ms(started_at),
                    "TOOL_EXECUTION_FAILED",
                )
            raise
        else:
            if not result_reported:
                await self.client.report_result(
                    tool_call_id,
                    "execution_unknown",
                    _duration_ms(started_at),
                    "TOOL_EXECUTION_RESULT_MISSING",
                )


class GatewayPermissionEngine:
    """Run Runtime preflight before the existing AgentScope Tool Guard."""

    def __init__(self, delegate: Any, middleware: BankRuntimeGatewayMiddleware) -> None:
        self.delegate = delegate
        self.middleware = middleware
        self.context = getattr(delegate, "context", None)
        self._trusted_sandbox_broker_tools: dict[int, tuple[Any, Any]] = {}

    def __getattr__(self, name: str) -> Any:
        return getattr(self.delegate, name)

    def trust_sandbox_broker_tools(self, tools: list[Any]) -> None:
        for tool in tools:
            if str(getattr(tool, "name", "") or "") not in _SANDBOX_BROKER_TOOLS:
                raise GatewayError("Unexpected sandbox broker tool registration")
            self._trusted_sandbox_broker_tools[id(tool)] = (
                tool,
                getattr(tool, "_func", None),
            )

    async def check_permission(
        self,
        tool: Any,
        tool_input: dict[str, Any],
    ) -> PermissionDecision:
        if self.middleware.configuration_error or self.middleware.client is None:
            return _deny("Runtime Tool Gateway is unavailable")
        tool_name = str(getattr(tool, "name", "") or "")
        trusted = self._trusted_sandbox_broker_tools.get(id(tool))
        is_trusted_broker = bool(
            tool_name in _SANDBOX_BROKER_TOOLS
            and trusted is not None
            and trusted[0] is tool
            and trusted[1] is getattr(tool, "_func", None)
        )
        if is_trusted_broker:
            decision = await self.delegate.check_permission(tool, tool_input)
            if decision.behavior == PermissionBehavior.ALLOW:
                self.middleware.prepare_broker(tool_name, tool_input)
                return decision
            if decision.behavior == PermissionBehavior.DENY:
                return decision
            return _deny("Interactive tool approval is unavailable on bank-runtime")
        call_id = f"call_{uuid.uuid4().hex}"
        try:
            preflight = await self.middleware.client.preflight(
                tool_name,
                tool_input,
                call_id=call_id,
            )
        except Exception:
            return _deny("Runtime Tool Gateway preflight denied this call")

        decision = await self.delegate.check_permission(tool, tool_input)
        blocked_boundary = tool_name in _BLOCKED_NESTED_TOOLS or bool(
            getattr(tool, "is_external_tool", False)
        )
        if blocked_boundary or decision.behavior == PermissionBehavior.DENY:
            await self._report_guard_safely(preflight, "block")
            return (
                decision
                if not blocked_boundary
                else _deny("This tool execution path is not managed by Bank Runtime")
            )
        if decision.behavior != PermissionBehavior.ALLOW:
            await self._report_guard_safely(preflight, "require_approval")
            return _deny("Interactive tool approval is unavailable on bank-runtime")
        self.middleware.prepare(tool_name, tool_input, preflight)
        return decision

    async def _report_guard_safely(
        self,
        preflight: Mapping[str, Any],
        decision: str,
    ) -> None:
        try:
            await self.middleware.client.report_guard(preflight, decision)
        except Exception:
            return


class BankRuntimeGatewayInstallHook(LifecycleHook):
    """Install Gateway permission ordering and remove unmediated executors."""

    phase = Phase.POST_AGENT_BUILD
    name = "bank_runtime_gateway_install"
    priority = 30

    async def run(self, ctx: HookContext) -> HookResult:
        if str(getattr(ctx.request, "channel", "") or "") != "bank-runtime":
            return HookResult()
        agent = ctx.agent
        if agent is None:
            raise GatewayError("Bank Runtime agent is unavailable")
        middleware = next(
            (
                item
                for item in getattr(agent, "_acting_middlewares", ())
                if isinstance(item, BankRuntimeGatewayMiddleware)
            ),
            None,
        )
        if middleware is None:
            raise GatewayError("Bank Runtime Gateway middleware is missing")
        if middleware.configuration_error:
            raise GatewayError(middleware.configuration_error)
        for group in getattr(getattr(agent, "toolkit", None), "tool_groups", ()):
            group.tools[:] = [tool for tool in group.tools if _is_managed_tool(tool)]
        engine = getattr(agent, "_engine", None)
        if engine is None:
            raise GatewayError("Bank Runtime Tool Guard is unavailable")
        if not isinstance(engine, GatewayPermissionEngine):
            agent._engine = GatewayPermissionEngine(engine, middleware)
        return HookResult()


def bank_runtime_middleware_factory(
    context: Any,
    agent_config: Any,
) -> BankRuntimeGatewayMiddleware | None:
    del agent_config
    request = getattr(context, "request", None)
    if str(getattr(request, "channel", "") or "") != "bank-runtime":
        return None
    request_context = getattr(request, "request_context", None)
    gateway = (
        request_context.get("runtime_tool_gateway")
        if isinstance(request_context, dict)
        else None
    )
    top_level = getattr(request, "runtime_tool_gateway", None)
    try:
        if not isinstance(gateway, Mapping) or not isinstance(top_level, Mapping):
            raise GatewayError("Runtime Tool Gateway configuration is missing")
        if dict(gateway) != dict(top_level):
            raise GatewayError("Runtime Tool Gateway configuration is inconsistent")
        config = GatewayConfig.from_mapping(gateway)
        trusted_agent_id = str(getattr(context, "agent_id", "") or "")
        if trusted_agent_id and config.agent_id != trusted_agent_id:
            raise GatewayError("Runtime Tool Gateway agent scope is invalid")
        sandbox_executor = RuntimeSandboxExecutor.from_request(
            base_url=config.base_url,
            sandbox_context=getattr(request, "sandbox_context", None),
        )
        return BankRuntimeGatewayMiddleware(
            GatewayClient(config),
            sandbox_executor=sandbox_executor,
            artifact_intent=artifact_delivery_intent_from_request(request),
        )
    except GatewayError as exc:
        return BankRuntimeGatewayMiddleware(
            None,
            configuration_error=str(exc),
            artifact_intent=artifact_delivery_intent_from_request(request),
        )


def _tool_call_names(response: Any) -> frozenset[str]:
    if not isinstance(response, ChatResponse):
        return frozenset()
    return frozenset(
        str(block.name or "")
        for block in response.content
        if isinstance(block, ToolCallBlock) and str(block.name or "")
    )


async def _capture_model_output(value: Any) -> _CapturedModelOutput:
    if isinstance(value, ChatResponse):
        return _CapturedModelOutput((value,), streamed=False)
    if not hasattr(value, "__aiter__"):
        raise TypeError("Model middleware returned an unsupported response")
    chunks = tuple([chunk async for chunk in value])
    if not chunks or any(not isinstance(chunk, ChatResponse) for chunk in chunks):
        raise TypeError("Streaming model middleware returned an invalid response")
    return _CapturedModelOutput(chunks, streamed=True)


def _visible_artifact_tool_schemas(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    selected: list[dict[str, Any]] = []
    for schema in value:
        if not isinstance(schema, dict):
            continue
        function = schema.get("function")
        name = str(function.get("name") or "") if isinstance(function, dict) else ""
        if name in ARTIFACT_WORKER_TOOL_NAMES:
            selected.append(schema)
    return selected


def _is_managed_tool(tool: Any) -> bool:
    name = str(getattr(tool, "name", "") or "")
    return name not in _BLOCKED_NESTED_TOOLS and not bool(
        getattr(tool, "is_external_tool", False)
    )


def _deny(message: str) -> PermissionDecision:
    return PermissionDecision(
        behavior=PermissionBehavior.DENY,
        message=message,
        decision_reason="bank-runtime Gateway fail-closed boundary",
    )


def _duration_ms(started_at: float) -> int:
    return max(int((time.monotonic() - started_at) * 1000), 0)


def _result_status(response: ToolResponse | ToolChunk) -> tuple[str, str]:
    state = getattr(response, "state", None)
    if state == ToolResultState.SUCCESS:
        return "completed", ""
    if state == ToolResultState.INTERRUPTED:
        return "execution_interrupted", "TOOL_EXECUTION_INTERRUPTED"
    return "failed", "TOOL_EXECUTION_FAILED"


def _sandbox_tool_response(
    response_id: str,
    tool_name: str,
    result: Mapping[str, Any],
) -> ToolResponse:
    state = ToolResultState.SUCCESS
    if tool_name in {"execute_shell_command", "shell.exec"}:
        exit_code = int(result.get("exit_code", 1) or 0)
        if exit_code != 0:
            state = ToolResultState.ERROR
        stdout = str(result.get("stdout") or "")[:262_144]
        stderr = str(result.get("stderr") or "")[:262_144]
        text = stdout or ("Command completed." if exit_code == 0 else "Command failed.")
        if stderr:
            text += f"\n[stderr]\n{stderr}"
    elif "content" in result and isinstance(result.get("content"), str):
        text = str(result["content"])[:262_144]
    else:
        safe = {
            key: value
            for key, value in result.items()
            if key
            not in {
                "token",
                "authorization",
                "headers",
                "bucket",
                "object_key",
                "read_url",
                "workspace_path",
            }
        }
        text = json.dumps(safe, ensure_ascii=False, sort_keys=True)[:262_144]
    return ToolResponse(
        id=response_id,
        content=[TextBlock(type="text", text=text)],
        state=state,
    )


def _runtime_tool_response(
    response_id: str,
    result: Mapping[str, Any],
) -> ToolResponse:
    status = str(result.get("status") or "")
    state = ToolResultState.SUCCESS if status == "success" else ToolResultState.ERROR
    safe = {
        key: value
        for key, value in result.items()
        if key
        in {
            "tool_call_id",
            "decision",
            "status",
            "reason",
            "error_code",
            "result",
        }
    }
    return ToolResponse(
        id=response_id,
        content=[
            TextBlock(
                type="text",
                text=json.dumps(safe, ensure_ascii=False, sort_keys=True)[:262_144],
            )
        ],
        state=state,
    )


__all__ = [
    "BankRuntimeGatewayInstallHook",
    "BankRuntimeGatewayMiddleware",
    "GatewayPermissionEngine",
    "bank_runtime_middleware_factory",
]
