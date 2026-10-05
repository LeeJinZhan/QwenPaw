"""Runtime-managed Session 2.0 isolation implemented at the plugin boundary."""

from __future__ import annotations

import asyncio
import copy
import json
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
_CTX_SCOPE_KEY = "bank_runtime_session_scope"
_DROP = object()
# Tool arguments/results are often JSON strings, not nested dictionaries.
# Redact both document handles and the actual offset-bearing cursor format on
# save AND load so existing sessions cannot feed task-local references back.
_RUNTIME_REFERENCE = re.compile(
    r"\b(?:(?:fr1|dr1|ds1|cur1)_[0-9a-f]{64}_[0-9a-f]{64}"
    r"|(?:cur1|cs1)_[0-9]+_[0-9a-f]{32}_[0-9a-f]{64})\b"
)


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
            scope.loaded_agent_state = _sanitize_agent_state(restored)
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
        ctx.extras[_CTX_SCOPE_KEY] = scope
        await scope.lock.acquire()
        scope.lock_acquired = True
        await store.prepare(
            scope,
            getattr(request, "session_bootstrap", None),
        )
        context = getattr(request, 'sandbox_context', None)
        if (isinstance(context, dict) and context.get('native_analysis_enabled') is True
                and context.get('isolation_level') == 'container'):
            # Trusted managed-history policy: raw Scroll archive recall would
            # bypass this session's task-safe projection. Request data alone
            # is insufficient; this marker follows verified session preparation.
            ctx.extras['managed_history_projection'] = True
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
        scope = ctx.extras.pop(_CTX_SCOPE_KEY, None)
        if isinstance(scope, ManagedSessionScope) and scope.lock_acquired:
            scope.lock.release()
            scope.lock_acquired = False
        token = ctx.extras.pop(_CTX_TOKEN_KEY, None)
        if isinstance(token, Token):
            try:
                _current_scope.reset(token)
            except ValueError:
                # Async generator finalization may run in another Context.
                if _current_scope.get() is scope:
                    _current_scope.set(None)
        for key in list(ctx.extras):
            if str(key).startswith("bank_runtime_session"):
                ctx.extras.pop(key, None)
        if isinstance(scope, ManagedSessionScope):
            ctx.extras.pop('managed_history_projection', None)
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
            message=Msg(name=role, role=role, content=blocks).to_dict()
            restored.append(message)
    if not restored:
        return None
    return _sanitize_agent_state({
        "state": {
            "session_id": session_id,
            "summary": "",
            "context": restored,
        }
    })


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
    # Only the saved/loaded snapshot is projected. The active task retains its
    # own tool feedback, while audit remains at the Gateway boundary.
    sanitized = _sanitize_value(_project_execution_history(copy.deepcopy(value)))
    return sanitized if isinstance(sanitized, dict) else {}


def _project_execution_history(value: Any) -> Any:
    from .sandbox.native_tools import NATIVE_TOOL_NAMES
    transient_names = NATIVE_TOOL_NAMES | {'recall_history', 'recall_history_python'}
    protocol_types = {'tool_call', 'tool_use', 'tool_result'}
    native_ids = set()

    def collect(item):
        if isinstance(item, list):
            for child in item:
                collect(child)
        elif isinstance(item, dict):
            if item.get('role') in {'assistant', 'tool'}:
                for block in item.get('content', []):
                    if (isinstance(block, dict) and block.get('type') in protocol_types
                            and block.get('name') in transient_names and block.get('id')):
                        native_ids.add(block['id'])
            for key, child in item.items():
                if key != 'content':
                    collect(child)

    collect(value)

    def without_native_preambles(messages):
        # Match the live phase contract using SDK ordering, before the tool
        # pairs disappear. Text before the last tool boundary in a native turn
        # is stage text; all trailing answer blocks remain verbatim. Never
        # guess from wording, block count, filenames or stored plain prose.
        result = list(messages)
        starts = [i for i, message in enumerate(messages)
                  if isinstance(message, dict) and message.get('role') == 'user']
        for start, end in zip([0, *starts], [*starts, len(messages)]):
            boundary = None
            native_turn = False
            for i in range(start, end):
                message = messages[i]
                if not isinstance(message, dict) or message.get('role') not in {'assistant', 'tool'}:
                    continue
                for j, block in enumerate(message.get('content', [])):
                    if isinstance(block, dict) and block.get('type') in protocol_types:
                        boundary = (i, j)
                        native_turn |= block.get('name') in transient_names or block.get('id') in native_ids
            if not native_turn or boundary is None:
                continue
            for i in range(start, boundary[0] + 1):
                message = messages[i]
                if not isinstance(message, dict) or message.get('role') != 'assistant':
                    continue
                content = message.get('content')
                if not isinstance(content, list):
                    continue
                kept = [block for j, block in enumerate(content) if not (
                    isinstance(block, dict) and block.get('type') == 'text'
                    and (i, j) < boundary)]
                if len(kept) != len(content):
                    result[i] = {**message, 'content': kept}
        return result

    def project(item):
        if isinstance(item, list):
            return [clean for child in without_native_preambles(item)
                    if (clean := project(child)) is not _DROP]
        if not isinstance(item, dict):
            return item
        if item.get('role') in {'assistant', 'tool'} and isinstance(item.get('content'), list):
            original = item['content']
            kept = [block for block in original if not (
                isinstance(block, dict) and (block.get('type') in {'thinking', 'reasoning'}
                or (block.get('type') in protocol_types
                    and (block.get('name') in transient_names or block.get('id') in native_ids))))]
            if len(kept) != len(original):
                if not kept:
                    return _DROP  # No empty SDK assistant/tool message after pair removal.
                item = {**item, 'content': kept}
        return {key: project(child) if key != 'content' else child for key, child in item.items()}

    return project(value)


def _sanitize_value(value: Any, *, tool_payload: bool = False, user_prose: bool = False,
                    ordinary_prose: bool = False) -> Any:
    if isinstance(value, list):
        result = []
        for item in value:
            clean = _sanitize_value(item, tool_payload=tool_payload, user_prose=user_prose, ordinary_prose=ordinary_prose)
            if clean is not _DROP:
                result.append(clean)
        return result
    if isinstance(value, str):
        if tool_payload:
            try:
                decoded = json.loads(value)
            except (ValueError, TypeError):
                decoded = None
            if isinstance(decoded, (dict, list)):
                return json.dumps(_sanitize_value(decoded, tool_payload=True), ensure_ascii=False)
        if user_prose and not tool_payload:
            return value
        if tool_payload or not ordinary_prose:
            value = re.sub(r'''/workspace/(?:input|scratch|output)/[^\s'"<>]+''', '历史任务路径已移除', value)
        return _RUNTIME_REFERENCE.sub("历史任务引用已移除", value).replace(
            "[runtime-reference-redacted]", "历史任务引用已移除")
    if not isinstance(value, dict):
        return value
    metadata = value.get('metadata')
    injected_ids = metadata.get('runtime_attachment_block_ids') if isinstance(metadata, dict) else None
    if isinstance(injected_ids, list) and isinstance(value.get('content'), list):
        known_ids = {item for item in injected_ids if isinstance(item, str)}
        value = {**value, 'content': [block for block in value['content']
            if not isinstance(block, dict) or block.get('id') not in known_ids]}
    if value.get('type') == 'tool_result' and value.get('name') == 'runtime_sandbox_files_select':
        from .sandbox.history import selected_file_metadata
        selected = selected_file_metadata(value)
        if selected:
            value = dict(value)
            metadata = value.get('metadata') if isinstance(value.get('metadata'), dict) else {}
            value['metadata'] = {**metadata, 'runtime_selected_file_metadata': selected}
    if value.get("_runtime_sandbox_attachment") is True:
        return _DROP
    tool_payload = tool_payload or value.get("type") in {"tool_use", "tool_call", "tool_result"}
    if _is_unsafe_file_block(value, tool_payload=tool_payload):
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
        "doc_ref",
        "cursor",
        "next_cursor",
        "group_cursor",
        "next_group_cursor",
        "read_url",
        "runtime_tool_gateway",
        "sandbox_context",
        "token",
        "container_path",
        "runtime_attachment_block_ids",
    }
    result: dict[str, Any] = {}
    for key, item in value.items():
        if str(key).lower() in forbidden and (tool_payload or not (user_prose or ordinary_prose)):
            continue
        clean = _sanitize_value(item, tool_payload=tool_payload,
            user_prose=user_prose or (key == 'content' and value.get('role') == 'user'),
            ordinary_prose=ordinary_prose or (key == 'content' and value.get('role') == 'assistant'))
        if clean is not _DROP:
            result[key] = clean
    return result


def _is_unsafe_file_block(value: dict[str, Any], *, tool_payload: bool = False) -> bool:
    block_type = str(value.get("type") or "").lower()
    if block_type == "text":
        text = str(value.get("text") or "")
        if tool_payload and "<runtime_attachment " in text:
            return True
        # Legacy saved sessions did not carry injected block IDs. Only a
        # standalone protocol wrapper with an actual task reference is a known
        # capability block; ordinary explanations of XML remain prose.
        wrapper = re.fullmatch(
            r'\s*<runtime_attachment\s+([^>]*)>'
            r'(?:(?!</?runtime_attachment(?:\s|>)).)*</runtime_attachment>\s*',
            text, flags=re.DOTALL)
        if wrapper:
            reference = re.search(r'''\bfile_ref\s*=\s*(["'])(.*?)\1''', wrapper.group(1))
            if reference and _RUNTIME_REFERENCE.fullmatch(reference.group(2)):
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
