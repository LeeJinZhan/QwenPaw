# -*- coding: utf-8 -*-
"""Deprecated compatibility symbol for the retired attachment-read tool."""
from __future__ import annotations

from agentscope.message import TextBlock
from agentscope.tool import ToolResponse


async def runtime_attachment_read(
    file_id: str,
    max_bytes: int = 8192,
) -> ToolResponse:
    """Return a stable deprecation response without reading any file.

    The function remains importable for one compatibility window, but it is
    no longer exported or registered in the model toolkit. Supplemental files
    must use Runtime metadata search followed by Worker-owned selection.
    """
    del file_id, max_bytes
    return ToolResponse(
        content=[
            TextBlock(
                type="text",
                text=(
                    "runtime_attachment_read is deprecated; use "
                    "runtime_sandbox_files_search followed by "
                    "runtime_sandbox_files_select."
                ),
            ),
        ],
    )
