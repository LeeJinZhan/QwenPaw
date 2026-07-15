# -*- coding: utf-8 -*-
"""Controlled bridge from QwenPaw to Bank Agent Runtime Tool Gateway."""
from __future__ import annotations

import asyncio
import json
import uuid
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urljoin, urlparse

from agentscope.message import TextBlock
from agentscope.tool import ToolResponse

from ...config.context import get_current_runtime_tool_gateway


async def runtime_tool_gateway(
    tool_id: str,
    input: dict[str, Any] | str | None = None,  # pylint: disable=redefined-builtin
    request_id: str = "",
    idempotency_key: str = "",
) -> ToolResponse:
    """Call a Runtime-approved tool through Runtime Tool Gateway.

    Args:
        tool_id: Runtime action id, such as ``workspace.list_outputs``.
        input: JSON object input for the Runtime action.
        request_id: Optional caller-provided request id for audit tracing.
        idempotency_key: Optional idempotency key for write/export actions.

    Returns:
        ToolResponse containing the public Runtime tool result.
    """
    gateway = get_current_runtime_tool_gateway()
    if not isinstance(gateway, dict):
        return _text_response(
            "Runtime Tool Gateway is not available for this request.",
        )

    normalized_tool_id = str(tool_id or "").strip()
    allowed_tools = _string_list(gateway.get("allowed_tools"))
    if normalized_tool_id not in allowed_tools:
        return _text_response(
            f"Runtime Tool Gateway tool '{normalized_tool_id}' is not allowed.",
        )

    url, url_error = _gateway_url(gateway)
    if url_error:
        return _text_response(url_error)

    token = str(gateway.get("token", "")).strip()
    if not token:
        return _text_response("Runtime Tool Gateway token is missing.")

    input_payload = _input_payload(input)
    payload = {
        "request_id": request_id
        or f"qwenpaw-{uuid.uuid4().hex}",
        "trace_id": str(gateway.get("trace_id", "")).strip(),
        "task_id": str(gateway.get("task_id", "")).strip(),
        "session_id": str(gateway.get("session_id", "")).strip(),
        "policy_snapshot_id": str(
            gateway.get("policy_snapshot_id", ""),
        ).strip(),
        "tool_session_id": str(gateway.get("tool_session_id", "")).strip(),
        "tool_id": normalized_tool_id,
        "idempotency_key": idempotency_key,
        "input": input_payload,
        **_first_allowed_skill_context(gateway, normalized_tool_id),
    }
    try:
        result = await asyncio.to_thread(
            _post_runtime_tool_gateway,
            url,
            token,
            payload,
            _timeout_seconds(gateway),
        )
    except RuntimeError as exc:
        return _text_response(str(exc))

    return _text_response(
        json.dumps(result, ensure_ascii=False, sort_keys=True),
    )


def _gateway_url(gateway: dict[str, Any]) -> tuple[str, str]:
    endpoint = str(gateway.get("endpoint", "")).strip()
    if not endpoint:
        return "", "Runtime Tool Gateway endpoint is missing."
    if urlparse(endpoint).scheme in {"http", "https"}:
        return endpoint, ""
    base_url = str(
        gateway.get("base_url") or gateway.get("runtime_base_url") or "",
    ).strip()
    if not base_url:
        return "", "Runtime Tool Gateway base_url is missing."
    return urljoin(base_url.rstrip("/") + "/", endpoint.lstrip("/")), ""


def _first_allowed_skill_context(
    gateway: dict[str, Any],
    tool_id: str,  # noqa: ARG001
) -> dict[str, str]:
    contexts = gateway.get("allowed_skill_contexts")
    if not isinstance(contexts, list):
        return {}
    required_keys = (
        "skill_code",
        "skill_version",
        "skill_catalog_version",
        "skill_binding_version",
        "worker_skill_id",
    )
    for context in contexts:
        if not isinstance(context, dict):
            continue
        normalized = {
            key: str(context.get(key, "")).strip()
            for key in required_keys
        }
        if all(normalized.values()):
            return normalized
    return {}


def _post_runtime_tool_gateway(
    url: str,
    token: str,
    payload: dict[str, Any],
    timeout_seconds: float,
) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-Tool-Session-Id": str(payload.get("tool_session_id", "")),
        },
    )
    try:
        with urllib.request.urlopen(
            request,
            timeout=timeout_seconds,
        ) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(
            f"Runtime Tool Gateway rejected the tool call: "
            f"HTTP {exc.code} {detail}",
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"Runtime Tool Gateway is unreachable: {exc.reason}",
        ) from exc

    try:
        parsed = json.loads(body) if body else {}
    except json.JSONDecodeError as exc:
        raise RuntimeError("Runtime Tool Gateway returned invalid JSON.") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("Runtime Tool Gateway returned a non-object body.")
    return parsed


def _timeout_seconds(gateway: dict[str, Any]) -> float:
    try:
        return max(float(gateway.get("timeout_seconds", 30)), 0.1)
    except (TypeError, ValueError):
        return 30.0


def _input_payload(input_value: dict[str, Any] | str | None) -> dict[str, Any]:
    if isinstance(input_value, dict):
        return input_value
    if not isinstance(input_value, str):
        return {}
    stripped = input_value.strip()
    if not stripped:
        return {}
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return {}
    if isinstance(parsed, dict):
        return parsed
    return {}


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _text_response(text: str) -> ToolResponse:
    return ToolResponse(content=[TextBlock(type="text", text=text)])
