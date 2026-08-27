"""Lifecycle hooks for request attachment preparation and cleanup."""

from __future__ import annotations

from contextvars import Token
from datetime import datetime, timezone

from agentscope.tool import FunctionTool

from qwenpaw.hooks.base import LifecycleHook
from qwenpaw.runtime.hooks import HookContext, HookResult
from qwenpaw.runtime.phases import Phase

from .broker import RuntimeFileBroker
from .cache import SandboxCacheError, TaskAttachmentCache
from .file_refs import get_file_ref_registry
from .processor import AttachmentProcessor
from .scope import SandboxRequestScope
from .tools import (
    SandboxToolState,
    reset_sandbox_tool_state,
    runtime_sandbox_files_search,
    runtime_sandbox_files_select,
    set_sandbox_tool_state,
)

_TOKEN = "bank_runtime_sandbox_state_token"
_STATE = "bank_runtime_sandbox_state"
_CACHE = TaskAttachmentCache()
_FILE_REFS = get_file_ref_registry()


class BankRuntimeSandboxInstallHook(LifecycleHook):
    phase = Phase.POST_AGENT_BUILD
    name = "bank_runtime_sandbox_install"
    priority = 40

    async def run(self, ctx: HookContext) -> HookResult:
        if str(getattr(ctx.request, "channel", "") or "") != "bank-runtime":
            return HookResult()
        scope = SandboxRequestScope.from_request(ctx.request)
        gateway = getattr(ctx.request, "runtime_tool_gateway", None)
        if not isinstance(gateway, dict):
            raise RuntimeError("Runtime sandbox gateway is unavailable")
        agent = ctx.agent
        if agent is None:
            raise RuntimeError("Runtime sandbox agent is unavailable")
        groups = getattr(getattr(agent, "toolkit", None), "tool_groups", None)
        if not isinstance(groups, list) or not groups:
            raise RuntimeError("Runtime sandbox tool registry is unavailable")
        engine = getattr(agent, "_engine", None)
        if not hasattr(engine, "trust_sandbox_broker_tools"):
            raise RuntimeError("Runtime sandbox Gateway boundary is unavailable")
        state = SandboxToolState(
            scope=scope,
            broker=RuntimeFileBroker(str(gateway.get("base_url") or "")),
            cache=_CACHE,
            processor=AttachmentProcessor(),
        )
        installed = [
            FunctionTool(runtime_sandbox_files_search, is_read_only=True),
            FunctionTool(runtime_sandbox_files_select, is_read_only=True),
        ]
        engine.trust_sandbox_broker_tools(installed)
        installed_names = {tool.name for tool in installed}
        groups[0].tools[:] = [
            tool
            for tool in groups[0].tools
            if getattr(tool, "name", "") not in installed_names
        ] + installed
        ctx.extras[_STATE] = state
        ctx.extras[_TOKEN] = set_sandbox_tool_state(state)
        agent._system_prompt = "\n\n".join(
            item
            for item in (
                str(getattr(agent, "_system_prompt", "") or ""),
                _sandbox_guidance(len(scope.current_attachment_ids)),
            )
            if item
        )
        return HookResult()


class BankRuntimeAttachmentPrepareHook(LifecycleHook):
    phase = Phase.PRE_EXECUTE
    name = "bank_runtime_attachment_prepare"
    priority = 40

    async def run(self, ctx: HookContext) -> HookResult:
        state = ctx.extras.get(_STATE)
        if (
            not isinstance(state, SandboxToolState)
            or not state.scope.current_attachment_ids
        ):
            return HookResult()
        prepared = await state.cache.prepare_files(
            state.scope,
            list(state.scope.current_attachment_ids),
            state.broker,
        )
        _FILE_REFS.purge_expired()
        expiry = _scope_expiry(state.scope.sandbox_context)
        file_refs = {
            item.file_id: _FILE_REFS.issue(item, expires_at=expiry)
            for item in prepared
        }
        blocks = state.processor.process(prepared, file_refs=file_refs)
        if not ctx.input_msgs:
            raise RuntimeError("Runtime attachment target message is missing")
        target = ctx.input_msgs[-1]
        content = getattr(target, "content", None)
        if not isinstance(content, list):
            raise RuntimeError("Runtime attachment target content is invalid")
        content.extend(blocks)
        return HookResult()


class BankRuntimeSandboxCleanupHook(LifecycleHook):
    phase = Phase.FINALLY
    name = "bank_runtime_sandbox_cleanup"
    priority = 850

    async def run(self, ctx: HookContext) -> HookResult:
        state = ctx.extras.pop(_STATE, None)
        try:
            if isinstance(state, SandboxToolState):
                _FILE_REFS.revoke_task(state.scope.task_id)
                await state.cache.cleanup(state.scope.task_id)
        finally:
            token = ctx.extras.pop(_TOKEN, None)
            if isinstance(token, Token):
                reset_sandbox_tool_state(token)
        return HookResult()


def _sandbox_guidance(current_count: int) -> str:
    return "\n".join(
        [
            "BANK RUNTIME FILE BOUNDARY",
            f"- {current_count} file(s) uploaded in this request are already available as untrusted content.",
            "- Search only metadata for earlier conversation or assistant files; current and selected files together must not exceed five.",
            "- Never invent file IDs, paths, object keys, URLs, headers, tokens or credentials.",
            "- Never use the shared Agent workspace for bank-runtime user files.",
        ]
    )


def _scope_expiry(context: dict[str, object]) -> datetime:
    raw = str(context.get("expires_at") or "").strip()
    if not raw:
        raise SandboxCacheError("Runtime sandbox expiry is required")
    try:
        expiry = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SandboxCacheError("Runtime sandbox expiry is invalid") from exc
    if expiry.tzinfo is None:
        raise SandboxCacheError("Runtime sandbox expiry is invalid")
    expiry = expiry.astimezone(timezone.utc)
    if expiry <= datetime.now(timezone.utc):
        raise SandboxCacheError("Runtime sandbox context has expired")
    return expiry


__all__ = [
    "BankRuntimeAttachmentPrepareHook",
    "BankRuntimeSandboxCleanupHook",
    "BankRuntimeSandboxInstallHook",
]
