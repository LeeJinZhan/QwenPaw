"""Runtime-managed Session 2.0 isolation implemented at the plugin boundary."""

from __future__ import annotations

import asyncio
import copy
import re
import threading
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any

from agentscope.message import Msg

from qwenpaw.exceptions import AgentRuntimeErrorException
from qwenpaw.agents.middlewares import MemoryMiddleware
from qwenpaw.hooks.base import LifecycleHook
from qwenpaw.runtime._state_utils import StateProxy
from qwenpaw.runtime.hooks import HookContext, HookResult
from qwenpaw.runtime.phases import Phase

_SCOPE_KEY = "bank_runtime_scope"
_CTX_TOKEN_KEY = "bank_runtime_session_context_token"
_DROP = object()
_RUNTIME_REFERENCE = re.compile(r"\b(?:fr1|dr1|cur1)_[0-9a-f]{64}_[0-9a-f]{64}\b")


class ManagedSessionError(AgentRuntimeErrorException):
    """Stable, sanitized error for the managed Session contract."""

    def __init__(self, error_code: str) -> None:
        messages = {
            "RUNTIME_SESSION_NOT_FOUND": "Managed session is unavailable",
            "RUNTIME_SESSION_SCOPE_MISMATCH": "Managed session scope is invalid",
            "RUNTIME_SESSION_REGENERATE_TARGET_MISMATCH": (
                "Managed session regenerate target is invalid"
            ),
            "RUNTIME_SESSION_REQUEST_INVALID": "Managed session request is invalid",
        }
        super().__init__(
            error_code=error_code,
            message=messages.get(error_code, "Managed session failed"),
            details={},
        )


@dataclass
class ManagedSessionScope:
    agent_id: str
    user_id: str
    channel: str
    session_id: str
    runtime_task_id: str
    declared_state: str
    operation: str
    regenerate_from_task_id: str
    lock: asyncio.Lock
    lock_acquired: bool = False
    loaded_agent_state: dict[str, Any] | None = None
    last_committed_task_id: str = ""
    duplicate_task: bool = False
    commit_allowed: bool = False
    committed: bool = False

    @property
    def lock_key(self) -> tuple[str, str, str, str]:
        return (
            self.agent_id,
            self.user_id,
            self.channel,
            self.session_id,
        )

    def matches_storage_call(
        self,
        session_id: str,
        user_id: str,
        channel: str,
    ) -> bool:
        return (
            self.session_id == str(session_id or "")
            and self.user_id == str(user_id or "")
            and self.channel == str(channel or "")
        )


_current_scope: ContextVar[ManagedSessionScope | None] = ContextVar(
    "bank_runtime_managed_session_scope",
    default=None,
)


def current_managed_session_scope() -> ManagedSessionScope | None:
    return _current_scope.get()


def _install_managed_session_store(workspace: Any) -> "ManagedSessionStore":
    """Install the wrapper on legacy and QwenPaw 2.1 workspaces."""

    current = getattr(workspace, "session", None)
    if isinstance(current, ManagedSessionStore):
        return current
    if current is None:
        raise ManagedSessionError("RUNTIME_SESSION_REQUEST_INVALID")

    store = ManagedSessionStore(current)
    service_manager = getattr(workspace, "_service_manager", None)
    services = getattr(service_manager, "services", None)
    if isinstance(services, dict) and services.get("session") is current:
        services["session"] = store
    else:
        try:
            setattr(workspace, "session", store)
        except (AttributeError, TypeError) as exc:
            raise ManagedSessionError("RUNTIME_SESSION_REQUEST_INVALID") from exc

    if getattr(workspace, "session", None) is not store:
        raise ManagedSessionError("RUNTIME_SESSION_REQUEST_INVALID")
    return store


class ManagedSessionStore:
    """Permanent workspace wrapper; request decisions live in ContextVar."""

    def __init__(self, delegate: Any) -> None:
        self.delegate = delegate
        self._locks: dict[tuple[str, str, str, str], asyncio.Lock] = {}
        self._locks_guard = threading.Lock()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.delegate, name)

    def lock_for(
        self,
        key: tuple[str, str, str, str],
    ) -> asyncio.Lock:
        with self._locks_guard:
            return self._locks.setdefault(key, asyncio.Lock())

    async def prepare(
        self,
        scope: ManagedSessionScope,
        bootstrap: Any,
    ) -> None:
        stored = await self.delegate.get_session_state_dict(
            session_id=scope.session_id,
            user_id=scope.user_id,
            channel=scope.channel,
            allow_not_exist=True,
        )
        if scope.declared_state == "stale":
            restored = _bootstrap_agent_state(scope.session_id, bootstrap)
            if restored is None:
                raise ManagedSessionError("RUNTIME_SESSION_REQUEST_INVALID")
            scope.loaded_agent_state = restored
            return
        if stored:
            marker = stored.get(_SCOPE_KEY)
            if not self._scope_marker_matches(scope, marker):
                raise ManagedSessionError("RUNTIME_SESSION_SCOPE_MISMATCH")
            agent_state = stored.get("agent")
            if not isinstance(agent_state, dict):
                raise ManagedSessionError("RUNTIME_SESSION_SCOPE_MISMATCH")
            scope.loaded_agent_state = _sanitize_agent_state(agent_state)
            scope.last_committed_task_id = str(
                marker.get("last_committed_task_id") or ""
            )
            scope.duplicate_task = (
                bool(scope.runtime_task_id)
                and scope.runtime_task_id == scope.last_committed_task_id
            )
            if scope.operation == "regenerate":
                if (
                    not scope.regenerate_from_task_id
                    or scope.regenerate_from_task_id != scope.last_committed_task_id
                ):
                    raise ManagedSessionError(
                        "RUNTIME_SESSION_REGENERATE_TARGET_MISMATCH"
                    )
                scope.loaded_agent_state = _rollback_last_turn(scope.loaded_agent_state)
            return

        if scope.declared_state == "active":
            raise ManagedSessionError("RUNTIME_SESSION_NOT_FOUND")
        scope.loaded_agent_state = _bootstrap_agent_state(
            scope.session_id,
            bootstrap,
        )

    async def load_session_state(
        self,
        session_id: str,
        user_id: str = "",
        channel: str = "",
        allow_not_exist: bool = True,
        **state_modules_mapping: Any,
    ) -> None:
        scope = _current_scope.get()
        if (
            scope is None
            or channel != "bank-runtime"
            or not scope.matches_storage_call(session_id, user_id, channel)
        ):
            await self.delegate.load_session_state(
                session_id=session_id,
                user_id=user_id,
                channel=channel,
                allow_not_exist=allow_not_exist,
                **state_modules_mapping,
            )
            return
        if scope.loaded_agent_state is None:
            return
        target = state_modules_mapping.get("agent")
        if target is not None:
            target.load_state_dict(copy.deepcopy(scope.loaded_agent_state))

    async def save_session_state(
        self,
        session_id: str,
        user_id: str = "",
        channel: str = "",
        **state_modules_mapping: Any,
    ) -> None:
        scope = _current_scope.get()
        if (
            scope is None
            or channel != "bank-runtime"
            or not scope.matches_storage_call(session_id, user_id, channel)
        ):
            await self.delegate.save_session_state(
                session_id=session_id,
                user_id=user_id,
                channel=channel,
                **state_modules_mapping,
            )
            return
        if not scope.commit_allowed or scope.committed or scope.duplicate_task:
            return
        agent = state_modules_mapping.get("agent")
        if agent is None:
            raise ManagedSessionError("RUNTIME_SESSION_REQUEST_INVALID")
        sanitized = _sanitize_agent_state(agent.state_dict())
        agent_proxy = StateProxy()
        agent_proxy.data = sanitized
        marker_proxy = StateProxy()
        marker_proxy.data = {
            "agent_id": scope.agent_id,
            "user_id": scope.user_id,
            "channel": scope.channel,
            "session_id": scope.session_id,
            "last_committed_task_id": scope.runtime_task_id,
        }
        await self.delegate.save_session_state(
            session_id=scope.session_id,
            user_id=scope.user_id,
            channel=scope.channel,
            agent=agent_proxy,
            **{_SCOPE_KEY: marker_proxy},
        )
        scope.last_committed_task_id = scope.runtime_task_id
        scope.committed = True
        scope.commit_allowed = False

    @staticmethod
    def _scope_marker_matches(
        scope: ManagedSessionScope,
        marker: Any,
    ) -> bool:
        if not isinstance(marker, dict):
            return False
        return all(
            str(marker.get(key) or "") == expected
            for key, expected in {
                "agent_id": scope.agent_id,
                "user_id": scope.user_id,
                "channel": scope.channel,
                "session_id": scope.session_id,
            }.items()
        )


class ManagedSessionPrepareHook(LifecycleHook):
    phase = Phase.PRE_AGENT_BUILD
    name = "bank_runtime_session_prepare"
    priority = 1

    async def run(self, ctx: HookContext) -> HookResult:
        request = ctx.request
        if str(getattr(request, "channel", "") or "") != "bank-runtime":
            return HookResult()
        values = {
            "agent_id": str(ctx.agent_id or "").strip(),
            "user_id": str(getattr(request, "user_id", "") or "").strip(),
            "channel": "bank-runtime",
            "session_id": str(ctx.session_id or "").strip(),
            "runtime_task_id": str(
                getattr(request, "runtime_task_id", "") or ""
            ).strip(),
        }
        if not all(values.values()):
            raise ManagedSessionError("RUNTIME_SESSION_REQUEST_INVALID")
        if str(getattr(request, "session_contract_version", "") or "") != "2.0":
            raise ManagedSessionError("RUNTIME_SESSION_REQUEST_INVALID")
        declared_state = str(
            getattr(request, "qwenpaw_session_state", "") or ""
        ).strip()
        if declared_state not in {"uninitialized", "active", "stale"}:
            raise ManagedSessionError("RUNTIME_SESSION_REQUEST_INVALID")
        operation = str(
            getattr(request, "session_operation", "append") or "append"
        ).strip()
        if operation not in {"append", "regenerate"}:
            raise ManagedSessionError("RUNTIME_SESSION_REQUEST_INVALID")

        store = _install_managed_session_store(ctx.workspace)
        key = (
            values["agent_id"],
            values["user_id"],
            values["channel"],
            values["session_id"],
        )
        scope = ManagedSessionScope(
            **values,
            declared_state=declared_state,
            operation=operation,
            regenerate_from_task_id=str(
                getattr(request, "regenerate_from_task_id", "") or ""
            ).strip(),
            lock=store.lock_for(key),
        )
        token = _current_scope.set(scope)
        ctx.extras[_CTX_TOKEN_KEY] = token
        await scope.lock.acquire()
        scope.lock_acquired = True
        await store.prepare(
            scope,
            getattr(request, "session_bootstrap", None),
        )
        return HookResult()


class ManagedSessionCommitHook(LifecycleHook):
    phase = Phase.POST_RESPONSE
    name = "bank_runtime_session_commit"
    priority = 80

    async def run(self, ctx: HookContext) -> HookResult:
        scope = _current_scope.get()
        if scope is not None and ctx.error is None and ctx.agent is not None:
            scope.commit_allowed = True
        return HookResult()


class ManagedSessionDisableLongTermMemoryHook(LifecycleHook):
    """Remove every long-term-memory request capability from bank agents."""

    phase = Phase.POST_AGENT_BUILD
    name = "bank_runtime_disable_long_term_memory"
    priority = 1

    _MIDDLEWARE_ATTRIBUTES = (
        "_reply_middlewares",
        "_reasoning_middlewares",
        "_acting_middlewares",
        "_model_call_middlewares",
        "_system_prompt_middlewares",
        "_compress_context_middlewares",
    )

    async def run(self, ctx: HookContext) -> HookResult:
        request = ctx.request
        if str(getattr(request, "channel", "") or "") != "bank-runtime":
            return HookResult()
        agent = ctx.agent
        memory_manager = getattr(ctx.workspace, "memory_manager", None)
        if agent is None or memory_manager is None:
            return HookResult()
        try:
            memory_tool_names = {
                name
                for tool in memory_manager.list_memory_tools()
                if (name := _tool_name(tool))
            }
            toolkit = getattr(agent, "toolkit", None)
            for group in getattr(toolkit, "tool_groups", ()):
                group.tools[:] = [
                    tool
                    for tool in group.tools
                    if _tool_name(tool) not in memory_tool_names
                ]
            for attribute in self._MIDDLEWARE_ATTRIBUTES:
                middlewares = getattr(agent, attribute, None)
                if isinstance(middlewares, list):
                    middlewares[:] = [
                        middleware
                        for middleware in middlewares
                        if not isinstance(middleware, MemoryMiddleware)
                    ]
            memory_prompt = str(memory_manager.get_memory_prompt() or "").strip()
            system_prompt = getattr(agent, "_system_prompt", None)
            if memory_prompt and isinstance(system_prompt, str):
                agent._system_prompt = system_prompt.replace(
                    memory_prompt,
                    "",
                ).strip()
        except Exception as exc:
            raise ManagedSessionError("RUNTIME_SESSION_REQUEST_INVALID") from exc
        return HookResult()


class ManagedSessionErrorHook(LifecycleHook):
    phase = Phase.ON_ERROR
    name = "bank_runtime_session_error"
    priority = 100

    async def run(self, ctx: HookContext) -> HookResult:
        if isinstance(ctx.error, ManagedSessionError):
            ctx.extras["_error_code"] = ctx.error.error_code
            ctx.extras["_error_text"] = ctx.error.message
        return HookResult()


class ManagedSessionCleanupHook(LifecycleHook):
    phase = Phase.FINALLY
    name = "bank_runtime_session_cleanup"
    priority = 1000

    async def run(self, ctx: HookContext) -> HookResult:
        scope = _current_scope.get()
        if scope is not None and scope.lock_acquired and scope.lock.locked():
            scope.lock.release()
            scope.lock_acquired = False
        token = ctx.extras.pop(_CTX_TOKEN_KEY, None)
        if isinstance(token, Token):
            _current_scope.reset(token)
        for key in list(ctx.extras):
            if str(key).startswith("bank_runtime_session"):
                ctx.extras.pop(key, None)
        return HookResult()


def _bootstrap_agent_state(
    session_id: str,
    bootstrap: Any,
) -> dict[str, Any] | None:
    if not isinstance(bootstrap, dict):
        return None
    messages = bootstrap.get("messages")
    if not isinstance(messages, list):
        return None
    restored: list[dict[str, Any]] = []
    for item in messages[:512]:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip().lower()
        if role not in {"user", "assistant"}:
            continue
        blocks: list[dict[str, str]] = []
        content = item.get("content")
        if isinstance(content, str) and content.strip():
            blocks.append({"type": "text", "text": content.strip()})
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "text":
                    continue
                text = str(block.get("text") or "").strip()
                if text:
                    blocks.append({"type": "text", "text": text})
        if blocks:
            restored.append(Msg(name=role, role=role, content=blocks).to_dict())
    if not restored:
        return None
    return {
        "state": {
            "session_id": session_id,
            "summary": "",
            "context": restored,
        }
    }


def _rollback_last_turn(state: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(state)
    agent_state = result.get("state")
    if not isinstance(agent_state, dict):
        return result
    context = agent_state.get("context")
    if not isinstance(context, list):
        return result
    for index in range(len(context) - 1, -1, -1):
        item = context[index]
        if isinstance(item, dict) and str(item.get("role") or "") == "user":
            agent_state["context"] = context[:index]
            break
    return result


def _sanitize_agent_state(value: Any) -> Any:
    sanitized = _sanitize_value(copy.deepcopy(value))
    return sanitized if isinstance(sanitized, dict) else {}


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, list):
        result = []
        for item in value:
            clean = _sanitize_value(item)
            if clean is not _DROP:
                result.append(clean)
        return result
    if isinstance(value, str):
        return _RUNTIME_REFERENCE.sub("[runtime-reference-redacted]", value)
    if not isinstance(value, dict):
        return value
    if value.get("_runtime_sandbox_attachment") is True:
        return _DROP
    if _is_unsafe_file_block(value):
        return _DROP
    forbidden = {
        "_runtime_attachment_file_id",
        "attachments_manifest",
        "authorization",
        "bucket",
        "locator",
        "object_key",
        "file_ref",
        "document_ref",
        "cursor",
        "read_url",
        "runtime_tool_gateway",
        "sandbox_context",
        "token",
    }
    result: dict[str, Any] = {}
    for key, item in value.items():
        if str(key).lower() in forbidden:
            continue
        clean = _sanitize_value(item)
        if clean is not _DROP:
            result[key] = clean
    return result


def _is_unsafe_file_block(value: dict[str, Any]) -> bool:
    block_type = str(value.get("type") or "").lower()
    if block_type == "text":
        text = str(value.get("text") or "")
        if "<runtime_attachment " in text:
            return True
    if block_type not in {
        "audio",
        "data",
        "file",
        "image",
        "video",
    }:
        return False
    for key in ("url", "file_url", "image_url", "video_url", "source"):
        item = value.get(key)
        rendered = str(item or "")
        if "file://" in rendered or "/task-files/" in rendered:
            return True
    return False


def _tool_name(tool: Any) -> str:
    return str(
        getattr(tool, "name", None) or getattr(tool, "__name__", None) or ""
    ).strip()


__all__ = [
    "ManagedSessionCleanupHook",
    "ManagedSessionCommitHook",
    "ManagedSessionDisableLongTermMemoryHook",
    "ManagedSessionError",
    "ManagedSessionErrorHook",
    "ManagedSessionPrepareHook",
    "ManagedSessionScope",
    "ManagedSessionStore",
    "current_managed_session_scope",
]
