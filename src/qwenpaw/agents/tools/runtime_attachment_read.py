# -*- coding: utf-8 -*-
"""Read Runtime-issued attachment grants by file_id only."""
from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlparse

from agentscope.message import TextBlock
from agentscope.tool import ToolResponse

from ...config.context import (
    get_current_runtime_attachments_manifest,
    get_current_runtime_tool_gateway,
)


DEFAULT_MAX_BYTES = 8192
MAX_BYTES_LIMIT = 20000


async def runtime_attachment_read(
    file_id: str,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> ToolResponse:
    """Read an attachment that Runtime already authorized for this request.

    The caller can only select a ``file_id`` from the hidden
    ``attachments_manifest``. The grant URL, headers and tokens are never
    accepted as tool arguments and are never returned in tool output.
    """
    normalized_file_id = str(file_id or "").strip()
    if not normalized_file_id:
        return _text_response("Runtime attachment file_id is required.")
    entry = _manifest_entry(normalized_file_id)
    if entry is None:
        return _text_response(
            f"Runtime attachment '{normalized_file_id}' is not available.",
        )
    read_url = str(entry.get("read_url", "")).strip()
    if not _safe_runtime_attachment_url(read_url, entry):
        return _text_response(
            f"Runtime attachment '{normalized_file_id}' is not readable.",
        )
    headers = _string_dict(entry.get("required_headers"))
    limit = _bounded_max_bytes(max_bytes)
    try:
        content, content_type = await asyncio.to_thread(
            _download_runtime_attachment,
            read_url,
            headers,
            _timeout_seconds(entry),
        )
    except RuntimeError:
        return _text_response(
            f"Runtime attachment '{normalized_file_id}' could not be read.",
        )
    preview = content[:limit].decode("utf-8", errors="replace")
    truncated = len(content) > limit
    result = {
        "file_id": normalized_file_id,
        "original_name": str(entry.get("original_name", "")).strip(),
        "content_type": str(entry.get("content_type") or content_type).strip(),
        "size_bytes": len(content),
        "preview_bytes": min(len(content), limit),
        "truncated": truncated,
        "content_preview": preview,
    }
    return _text_response(json.dumps(result, ensure_ascii=False, sort_keys=True))


def _manifest_entry(file_id: str) -> dict[str, Any] | None:
    manifest = get_current_runtime_attachments_manifest()
    if not isinstance(manifest, list):
        return None
    for item in manifest:
        if not isinstance(item, dict):
            continue
        if str(item.get("file_id", "")).strip() == file_id:
            return item
    return None


def _download_runtime_attachment(
    url: str,
    headers: dict[str, str],
    timeout_seconds: float,
) -> tuple[bytes, str]:
    request = urllib.request.Request(
        url,
        method="GET",
        headers=headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            content = response.read()
            content_type = str(response.headers.get("Content-Type") or "")
            return content, content_type
    except urllib.error.HTTPError as exc:
        raise RuntimeError("Runtime attachment read was rejected.") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError("Runtime attachment read endpoint is unreachable.") from exc


def _safe_runtime_attachment_url(url: str, entry: dict[str, Any]) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    access_mode = str(entry.get("access_mode", "")).strip()
    if access_mode not in {"", "runtime_read_url"}:
        return False
    if parsed.path.startswith("/runtime/internal/file-grants/") and parsed.path.endswith("/content"):
        return _same_origin(parsed, _runtime_origin())
    return False


def _runtime_origin() -> tuple[str, str, int] | None:
    gateway = get_current_runtime_tool_gateway()
    if not isinstance(gateway, dict):
        return None
    endpoint = str(gateway.get("endpoint", "")).strip()
    if urlparse(endpoint).scheme in {"http", "https"}:
        return _origin_tuple(urlparse(endpoint))
    base_url = str(gateway.get("base_url") or gateway.get("runtime_base_url") or "").strip()
    if not base_url:
        return None
    return _origin_tuple(urlparse(base_url))


def _same_origin(parsed_url, origin: tuple[str, str, int] | None) -> bool:
    if origin is None:
        return False
    parsed_origin = _origin_tuple(parsed_url)
    return parsed_origin == origin


def _origin_tuple(parsed_url) -> tuple[str, str, int]:
    scheme = str(parsed_url.scheme or "").lower()
    host = str(parsed_url.hostname or "").lower()
    port = parsed_url.port or (443 if scheme == "https" else 80)
    return scheme, host, port


def _string_dict(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key): str(item)
        for key, item in value.items()
        if str(key).strip() and str(item).strip()
    }


def _bounded_max_bytes(value: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = DEFAULT_MAX_BYTES
    return min(max(parsed, 1), MAX_BYTES_LIMIT)


def _timeout_seconds(entry: dict[str, Any]) -> float:
    try:
        return max(float(entry.get("timeout_seconds", 30)), 0.1)
    except (TypeError, ValueError):
        return 30.0


def _text_response(text: str) -> ToolResponse:
    return ToolResponse(content=[TextBlock(type="text", text=text)])
