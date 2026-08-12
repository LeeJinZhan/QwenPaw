# -*- coding: utf-8 -*-
"""Select, materialize, and locally process Runtime-discovered files."""
from __future__ import annotations

import asyncio
import json
from typing import Any

from agentscope.message import TextBlock
from agentscope.tool import ToolResponse

from ...config.context import (
    get_current_runtime_discovered_files,
    get_current_runtime_sandbox_context,
    get_current_runtime_selected_file_ids,
    merge_current_runtime_discovered_files,
    set_current_runtime_selected_file_ids,
)
from . import runtime_sandbox_oss


MAX_RUNTIME_AUTONOMOUS_SELECTION_FILES = 3


async def runtime_sandbox_files_select(file_ids: list[str]) -> ToolResponse:
    """Load files returned by this request's Runtime metadata search.

    The model cannot provide paths or storage locators. Every file ID must be
    present in the request-local search registry, and Runtime batch-authorizes
    the selected IDs again before the Worker downloads and processes them.
    """
    sandbox_context = get_current_runtime_sandbox_context()
    if not isinstance(sandbox_context, dict):
        return _text_response("Runtime sandbox file selection is unavailable.")
    try:
        ordered = _validated_selection(file_ids)
    except (TypeError, ValueError) as exc:
        return _text_response(str(exc))

    discovered = get_current_runtime_discovered_files()
    unavailable = [file_id for file_id in ordered if file_id not in discovered]
    if unavailable:
        await _refresh_request_local_candidates(sandbox_context)
        discovered = get_current_runtime_discovered_files()
        unavailable = [file_id for file_id in ordered if file_id not in discovered]
    if unavailable:
        return _text_response(
            "The requested file could not be matched to the files visible in this "
            "request. Search the visible file metadata again before selecting it. "
            "If it remains unavailable, ask the user which displayed file they mean. "
            "Do not try another file-reading method."
        )

    selected = get_current_runtime_selected_file_ids()
    new_ids = [file_id for file_id in ordered if file_id not in selected]
    if len(selected) + len(new_ids) > MAX_RUNTIME_AUTONOMOUS_SELECTION_FILES:
        return _text_response("Too many Runtime files were selected for one request.")
    if not new_ids:
        return _text_response("The selected Runtime files are already available.")

    task_id = str(sandbox_context.get("task_id") or "").strip()
    if not task_id:
        return _text_response("Runtime sandbox file selection is unavailable.")
    cache = runtime_sandbox_oss._DEFAULT_TASK_ATTACHMENT_CACHE
    # Import lazily because the attachment processor itself depends on the
    # tools package for the task-local cache types.
    from ..attachments import (  # pylint: disable=import-outside-toplevel
        RuntimeAttachmentProcessingError,
        RuntimeAttachmentProcessor,
    )

    selection_records = [
        {
            "file_id": file_id,
            "source": str(discovered[file_id].get("source") or ""),
            "selection_mode": "model_metadata_selection",
        }
        for file_id in new_ids
    ]
    try:
        prepared_files = await runtime_sandbox_oss.run_task_io_in_thread(
            cache,
            task_id,
            cache.prepare_files,
            new_ids,
            sandbox_context,
            selection_records=selection_records,
            file_id=new_ids[0],
        )
        result = await runtime_sandbox_oss.run_task_io_in_thread(
            cache,
            task_id,
            RuntimeAttachmentProcessor().process,
            prepared_files,
            file_id=new_ids[0],
        )
    except (
        runtime_sandbox_oss.RuntimeAttachmentPreparationError,
        RuntimeAttachmentProcessingError,
        OSError,
        RuntimeError,
        ValueError,
    ):
        return _text_response("Runtime sandbox file selection failed.")

    set_current_runtime_selected_file_ids(selected.union(new_ids))
    safe_selection = [
        {
            "file_id": file_id,
            "display_name": str(discovered[file_id].get("display_name") or "")[:255],
            "source": str(discovered[file_id].get("source") or "")[:64],
        }
        for file_id in new_ids
    ]
    summary = TextBlock(
        type="text",
        text=json.dumps(
            {"selected_files": safe_selection, "warnings": result.warnings},
            ensure_ascii=False,
            sort_keys=True,
        ),
    )
    return ToolResponse(content=[summary, *result.content_parts])


async def _refresh_request_local_candidates(
    sandbox_context: dict[str, Any],
) -> None:
    """Revalidate a session-carried ID against this request's user scope."""
    from . import runtime_sandbox_files  # pylint: disable=import-outside-toplevel

    try:
        result = await asyncio.to_thread(
            runtime_sandbox_oss.SandboxedOssClient().search_files,
            "",
            [],
            ["conversation", "assistant_workspace"],
            20,
            sandbox_context,
        )
        files = runtime_sandbox_files._without_current_task_files(
            runtime_sandbox_files._public_files(result),
        )
    except (RuntimeError, TypeError, ValueError, OSError):
        return
    merge_current_runtime_discovered_files(
        [item for item in files if item.get("readable") is True],
    )


def _validated_selection(value: Any) -> list[str]:
    if not isinstance(value, list):
        raise TypeError("Runtime file selection requires a file_id list.")
    ordered: list[str] = []
    for item in value:
        file_id = str(item or "").strip()
        if not file_id or file_id in ordered:
            continue
        ordered.append(file_id)
    if not ordered:
        raise ValueError("Runtime file selection requires at least one file_id.")
    if len(ordered) > MAX_RUNTIME_AUTONOMOUS_SELECTION_FILES:
        raise ValueError("Too many Runtime files were selected for one request.")
    return ordered


def _text_response(text: str) -> ToolResponse:
    return ToolResponse(content=[TextBlock(type="text", text=text)])
