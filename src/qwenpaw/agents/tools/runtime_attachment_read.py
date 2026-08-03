# -*- coding: utf-8 -*-
"""Read Runtime-authorized sandbox attachments by file_id only."""
from __future__ import annotations

import json
from typing import Any

from agentscope.message import TextBlock
from agentscope.tool import ToolResponse

from ...config.context import (
    get_current_runtime_attachments_manifest,
    get_current_runtime_discovered_file_ids,
    get_current_runtime_sandbox_context,
)
from . import runtime_sandbox_oss
from .runtime_sandbox_oss import SandboxedObjectContent, SandboxedOssClient


DEFAULT_MAX_BYTES = 8192
MAX_BYTES_LIMIT = 20000


async def runtime_attachment_read(
    file_id: str,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> ToolResponse:
    """Read a request-discovered supplemental file by ``file_id``.

    Current-task files are materialized and processed before the first model
    call, so this tool accepts only a ``file_id`` returned by
    ``runtime_sandbox_files_search`` in this request.
    Runtime re-authorizes the read with the request sandbox context, and
    locators, object keys, URLs, headers and credentials are never accepted as
    tool arguments or returned in tool output.
    """
    normalized_file_id = str(file_id or "").strip()
    if not normalized_file_id:
        return _text_response("Runtime attachment file_id is required.")
    discovered = normalized_file_id in get_current_runtime_discovered_file_ids()
    if not discovered:
        return _text_response(
            f"Runtime attachment '{normalized_file_id}' is not available.",
        )
    entry = _manifest_entry(normalized_file_id)
    if entry is not None and str(entry.get("access_mode", "")).strip() != (
        "sandbox_oss"
    ):
        return _text_response(
            f"Runtime attachment '{normalized_file_id}' is not readable.",
        )
    sandbox_context = get_current_runtime_sandbox_context()
    if not isinstance(sandbox_context, dict):
        return _text_response(
            f"Runtime attachment '{normalized_file_id}' is not readable.",
        )
    limit = _bounded_max_bytes(max_bytes)
    try:
        cache = runtime_sandbox_oss._DEFAULT_TASK_ATTACHMENT_CACHE
        client = SandboxedOssClient()
        sandboxed_content = await runtime_sandbox_oss.run_task_io_in_thread(
            cache,
            str(sandbox_context.get("task_id") or "").strip(),
            client.read_file,
            normalized_file_id,
            sandbox_context,
            max_bytes=limit + 1,
            file_id=normalized_file_id,
        )
        content = sandboxed_content.content
        preview_content = content[:limit]
        preview = preview_content.decode("utf-8", errors="replace")
        truncated = sandboxed_content.size_bytes > limit
        result = {
            "file_id": normalized_file_id,
            "original_name": str(
                entry.get("original_name", "") if entry is not None else "",
            ).strip(),
            "content_type": str(
                (
                    entry.get("content_type")
                    if entry is not None
                    else ""
                )
                or sandboxed_content.content_type,
            ).strip(),
            "size_bytes": sandboxed_content.size_bytes,
            "preview_bytes": len(preview_content),
            "truncated": truncated,
            "content_preview": preview,
        }
        return _text_response(
            json.dumps(result, ensure_ascii=False, sort_keys=True),
        )
    except Exception:
        return _text_response("Runtime attachment could not be read.")


def _manifest_entry(file_id: str) -> dict[str, Any] | None:
    manifest = get_current_runtime_attachments_manifest()
    if not isinstance(manifest, list):
        return None
    for item in manifest:
        if not isinstance(item, dict):
            continue
        if (
            str(item.get("source", "")).strip() == "current_task"
            and str(item.get("file_id", "")).strip() == file_id
        ):
            return item
    return None


def _bounded_max_bytes(value: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = DEFAULT_MAX_BYTES
    return min(max(parsed, 1), MAX_BYTES_LIMIT)


def _text_response(text: str) -> ToolResponse:
    return ToolResponse(content=[TextBlock(type="text", text=text)])
