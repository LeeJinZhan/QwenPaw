# -*- coding: utf-8 -*-
"""Discover supplemental files through the signed Runtime sandbox."""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from agentscope.message import TextBlock
from agentscope.tool import ToolResponse

from ...config.context import (
    get_current_runtime_discovered_file_ids,
    get_current_runtime_sandbox_context,
    set_current_runtime_discovered_file_ids,
)
from .runtime_sandbox_oss import SandboxedOssClient


_PUBLIC_FILE_FIELDS = (
    "file_id",
    "display_name",
    "content_type",
    "size_bytes",
    "created_at",
    "source",
    "readable",
    "status_label",
)
_FILE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_CONTENT_TYPE_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9!#$&^_.+*-]*/[A-Za-z0-9][A-Za-z0-9!#$&^_.+*-]*$",
)


async def runtime_sandbox_files_search(
    query: str,
    content_types: list[str] | None = None,
    sources: list[str] | None = None,
    limit: int = 20,
) -> ToolResponse:
    """Search supplemental files visible to the current Runtime sandbox."""
    sandbox_context = get_current_runtime_sandbox_context()
    if not isinstance(sandbox_context, dict):
        return _text_response("Runtime sandbox file search is unavailable.")
    try:
        safe_query = _safe_query(query)
        safe_content_types = _safe_content_types(content_types)
        safe_sources = _safe_sources(sources)
        safe_limit = _safe_limit(limit)
        result = await asyncio.to_thread(
            SandboxedOssClient().search_files,
            safe_query,
            safe_content_types,
            safe_sources,
            safe_limit,
            sandbox_context,
        )
        files = _public_files(result)
    except (RuntimeError, TypeError, ValueError):
        return _text_response("Runtime sandbox file search failed.")

    discovered = get_current_runtime_discovered_file_ids().union(
        item["file_id"] for item in files if item["readable"] is True
    )
    set_current_runtime_discovered_file_ids(discovered)
    return _text_response(
        json.dumps({"files": files}, ensure_ascii=False, sort_keys=True),
    )


def _safe_query(value: Any) -> str:
    query = str(value or "").strip()
    if len(query) > 200 or any(ord(character) < 32 for character in query):
        raise ValueError("Runtime sandbox file query is invalid.")
    return query


def _safe_content_types(value: list[str] | None) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise TypeError("Runtime sandbox content types are invalid.")
    normalized: list[str] = []
    for item in value:
        content_type = str(item or "").strip().lower()
        if not content_type or not _CONTENT_TYPE_PATTERN.fullmatch(content_type):
            raise ValueError("Runtime sandbox content type is invalid.")
        if content_type not in normalized:
            normalized.append(content_type)
        if len(normalized) > 20:
            raise ValueError("Too many Runtime sandbox content types.")
    return normalized


def _safe_limit(value: int) -> int:
    try:
        return min(max(int(value), 1), 50)
    except (TypeError, ValueError):
        return 20


def _safe_sources(value: list[str] | None) -> list[str]:
    if value is None:
        return ["conversation", "assistant_workspace"]
    if not isinstance(value, list):
        raise TypeError("Runtime sandbox sources are invalid.")
    allowed = {"conversation", "assistant_workspace"}
    normalized: list[str] = []
    for item in value:
        source = str(item or "").strip()
        if source not in allowed:
            raise ValueError("Runtime sandbox source is invalid.")
        if source not in normalized:
            normalized.append(source)
    return normalized


def _public_files(result: Any) -> list[dict[str, Any]]:
    if not isinstance(result, dict):
        raise RuntimeError("Runtime returned invalid sandbox search results.")
    raw_files = result.get("files", [])
    if not isinstance(raw_files, list):
        raise RuntimeError("Runtime returned invalid sandbox search results.")
    files: list[dict[str, Any]] = []
    for item in raw_files:
        if not isinstance(item, dict):
            continue
        file_id = str(item.get("file_id") or "").strip()
        if not _FILE_ID_PATTERN.fullmatch(file_id):
            continue
        public_item = {field: item.get(field) for field in _PUBLIC_FILE_FIELDS}
        public_item["file_id"] = file_id
        public_item["display_name"] = str(
            public_item.get("display_name") or "",
        )[:255]
        public_item["content_type"] = str(
            public_item.get("content_type") or "",
        )[:128]
        public_item["created_at"] = str(
            public_item.get("created_at") or "",
        )[:64]
        public_item["source"] = str(public_item.get("source") or "")[:64]
        public_item["readable"] = public_item.get("readable") is True
        public_item["status_label"] = str(
            public_item.get("status_label") or "",
        )[:64]
        try:
            public_item["size_bytes"] = max(
                int(public_item.get("size_bytes") or 0),
                0,
            )
        except (TypeError, ValueError):
            public_item["size_bytes"] = 0
        files.append(public_item)
    return files


def _text_response(text: str) -> ToolResponse:
    return ToolResponse(content=[TextBlock(type="text", text=text)])
