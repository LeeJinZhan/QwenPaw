# -*- coding: utf-8 -*-
"""Expand earlier messages from the Runtime-authoritative conversation."""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from agentscope.message import TextBlock
from agentscope.tool import ToolResponse

from ...config.context import get_current_runtime_sandbox_context
from .runtime_sandbox_oss import SandboxedOssClient


_TURN_ID = re.compile(r"^turn_[A-Za-z0-9_-]{1,128}$")


async def conversation_context_expand(
    before_turn_id: str,
    limit: int = 8,
) -> ToolResponse:
    """Fetch a bounded earlier slice from this task's current conversation."""
    sandbox_context = get_current_runtime_sandbox_context()
    if not isinstance(sandbox_context, dict):
        return _response("Conversation context expansion is unavailable.")
    boundary = str(before_turn_id or "").strip()
    if not _TURN_ID.fullmatch(boundary):
        return _response("Conversation context expansion failed.")
    try:
        safe_limit = min(max(int(limit or 8), 1), 32)
    except (TypeError, ValueError):
        safe_limit = 8
    try:
        result = await asyncio.to_thread(
            SandboxedOssClient().expand_conversation_context,
            boundary,
            safe_limit,
            sandbox_context,
        )
        public = _public_result(result)
    except (RuntimeError, TypeError, ValueError):
        return _response("Conversation context expansion failed.")
    return _response(json.dumps(public, ensure_ascii=False, sort_keys=True))


def _public_result(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict) or not isinstance(result.get("messages", []), list):
        raise RuntimeError("invalid conversation context response")
    messages: list[dict[str, Any]] = []
    for item in result.get("messages", [])[:32]:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "")
        if role not in {"user", "assistant"}:
            continue
        content = item.get("content", [])
        if not isinstance(content, list):
            continue
        safe_parts = [
            {"type": "text", "text": str(part.get("text") or "")[:16000]}
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        ]
        if safe_parts:
            messages.append({"role": role, "content": safe_parts})
    next_boundary = str(result.get("next_before_turn_id") or "").strip()
    if next_boundary and not _TURN_ID.fullmatch(next_boundary):
        next_boundary = ""
    return {
        "messages": messages,
        "has_more": result.get("has_more") is True,
        "next_before_turn_id": next_boundary,
    }


def _response(text: str) -> ToolResponse:
    return ToolResponse(content=[TextBlock(type="text", text=text)])
