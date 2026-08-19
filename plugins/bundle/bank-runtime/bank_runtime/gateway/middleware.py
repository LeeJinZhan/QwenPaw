"""Gateway-first permission decorator and unique raw execution middleware."""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass
import asyncio
import json
import time
from typing import Any, Mapping
import uuid

from agentscope.message import ToolResultState
from agentscope.middleware import MiddlewareBase
from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.tool import ToolChunk, ToolResponse

from qwenpaw.hooks.base import LifecycleHook
from qwenpaw.runtime.hooks import HookContext, HookResult
from qwenpaw.runtime.phases import Phase

from .client import GatewayClient, GatewayConfig, GatewayError
from .protocol import canonical_payload_hash

_BLOCKED_NESTED_TOOLS = frozenset({"run_tool_batch"})


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
        configuration_error: str = "",
    ) -> None:
        self.client = client
        self.configuration_error = str(configuration_error or "")
        self._prepared: dict[tuple[str, str], deque[_PreparedExecution]] = defaultdict(
            deque
        )

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
        prepared = self.claim(tool_name, tool_input)
        try:
            await self.client.report_guard(prepared.preflight, "allow")
        except Exception as exc:
            raise GatewayError("Runtime Tool Guard acknowledgement failed") from exc
        tool_call_id = str(prepared.preflight.get("tool_call_id") or "")
        started_at = time.monotonic()
        result_reported = False
        try:
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

    def __getattr__(self, name: str) -> Any:
        return getattr(self.delegate, name)

    async def check_permission(
        self,
        tool: Any,
        tool_input: dict[str, Any],
    ) -> PermissionDecision:
        if self.middleware.configuration_error or self.middleware.client is None:
            return _deny("Runtime Tool Gateway is unavailable")
        tool_name = str(getattr(tool, "name", "") or "")
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
        return BankRuntimeGatewayMiddleware(GatewayClient(config))
    except GatewayError as exc:
        return BankRuntimeGatewayMiddleware(None, configuration_error=str(exc))


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


__all__ = [
    "BankRuntimeGatewayInstallHook",
    "BankRuntimeGatewayMiddleware",
    "GatewayPermissionEngine",
    "bank_runtime_middleware_factory",
]
