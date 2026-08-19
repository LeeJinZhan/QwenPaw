"""Request-scoped metadata search and explicit file selection tools."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
import json
import re
from typing import Any

from agentscope.message import TextBlock
from agentscope.tool import ToolResponse

from .broker import RuntimeFileBroker
from .cache import TaskAttachmentCache
from .processor import AttachmentProcessor
from .scope import SandboxRequestScope, SandboxScopeError

_EXTENSION = re.compile(r"^\.[a-z0-9][a-z0-9.+_-]{0,31}$")
_CONTENT_TYPE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9!#$&^_.+*-]*/[A-Za-z0-9][A-Za-z0-9!#$&^_.+*-]*$"
)


@dataclass
class SandboxToolState:
    scope: SandboxRequestScope
    broker: RuntimeFileBroker
    cache: TaskAttachmentCache
    processor: AttachmentProcessor


_STATE: ContextVar[SandboxToolState | None] = ContextVar(
    "bank_runtime_sandbox_tools", default=None
)


def set_sandbox_tool_state(value: SandboxToolState | None):
    return _STATE.set(value)


def reset_sandbox_tool_state(token) -> None:
    _STATE.reset(token)


async def runtime_sandbox_files_search(
    query: str = "",
    content_types: list[str] | None = None,
    sources: list[str] | None = None,
    limit: int = 20,
    extensions: list[str] | None = None,
) -> ToolResponse:
    state = _STATE.get()
    if state is None:
        return _text("Runtime file search is unavailable.")
    try:
        safe_query = str(query or "").strip()
        if len(safe_query) > 200 or any(ord(char) < 32 for char in safe_query):
            raise SandboxScopeError("Runtime file search query is invalid")
        safe_types = _validated_list(content_types, _CONTENT_TYPE, 20)
        safe_extensions = _validated_extensions(extensions)
        safe_sources = list(sources or ["conversation", "assistant_workspace"])
        if any(
            item not in {"conversation", "assistant_workspace"} for item in safe_sources
        ):
            raise SandboxScopeError("Runtime file search source is invalid")
        files = await state.broker.search(
            state.scope,
            query=safe_query,
            content_types=safe_types,
            extensions=safe_extensions,
            sources=list(dict.fromkeys(safe_sources)),
            limit=min(max(int(limit), 1), 50),
        )
        state.scope.remember_discovered(files)
        public = [
            state.scope.discovered_files[key] for key in state.scope.discovered_files
        ]
        return _text(json.dumps({"files": public}, ensure_ascii=False, sort_keys=True))
    except (RuntimeError, TypeError, ValueError):
        return _text("Runtime file search failed.")


async def runtime_sandbox_files_select(file_ids: list[str]) -> ToolResponse:
    state = _STATE.get()
    if state is None:
        return _text("Runtime file selection is unavailable.")
    try:
        records = state.scope.selection_records(file_ids)
        prepared = await state.cache.prepare_files(
            state.scope,
            list(file_ids),
            state.broker,
            selection_records=records,
        )
        blocks = state.processor.process(prepared)
        state.scope.mark_selected(list(file_ids))
        selected = [
            {
                "file_id": record["file_id"],
                "display_name": state.scope.discovered_files[record["file_id"]][
                    "display_name"
                ],
                "source": record["source"],
            }
            for record in records
        ]
        return ToolResponse(
            content=[
                TextBlock(
                    type="text",
                    text=json.dumps(
                        {"selected_files": selected}, ensure_ascii=False, sort_keys=True
                    ),
                ),
                *blocks,
            ]
        )
    except (RuntimeError, TypeError, ValueError):
        return _text("Runtime file selection failed.")


def _validated_list(value: Any, pattern: re.Pattern[str], limit: int) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > limit:
        raise SandboxScopeError("Runtime file search filter is invalid")
    result = [str(item or "").strip().lower() for item in value]
    if any(not pattern.fullmatch(item) for item in result):
        raise SandboxScopeError("Runtime file search filter is invalid")
    return list(dict.fromkeys(result))


def _validated_extensions(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 20:
        raise SandboxScopeError("Runtime extension filter is invalid")
    result = []
    for item in value:
        extension = str(item or "").strip().lower()
        if extension and not extension.startswith("."):
            extension = f".{extension}"
        if not _EXTENSION.fullmatch(extension):
            raise SandboxScopeError("Runtime extension filter is invalid")
        result.append(extension)
    return list(dict.fromkeys(result))


def _text(value: str) -> ToolResponse:
    return ToolResponse(content=[TextBlock(type="text", text=value)])


__all__ = [
    "SandboxToolState",
    "reset_sandbox_tool_state",
    "runtime_sandbox_files_search",
    "runtime_sandbox_files_select",
    "set_sandbox_tool_state",
]
