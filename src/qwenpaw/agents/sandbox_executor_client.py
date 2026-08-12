# -*- coding: utf-8 -*-
"""Permit-bound client for Runtime-owned per-task execution sandboxes."""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ..config.runtime_endpoint import resolve_runtime_base_url
from ..config.context import (
    get_current_runtime_sandbox_context,
    get_current_runtime_tool_execution,
    get_current_runtime_tool_gateway,
)


class SandboxExecutorClientError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        reason_code: str = "",
        trace_id: str = "",
    ) -> None:
        self.reason_code = _safe_error_token(reason_code)
        self.trace_id = _safe_error_token(trace_id, uppercase=False)
        super().__init__(message)


def _safe_error_token(value: Any, *, uppercase: bool = True) -> str:
    normalized = str(value or "").strip()
    if not normalized or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", normalized):
        return ""
    return normalized.upper() if uppercase else normalized


def _runtime_http_error(message: str, error: HTTPError) -> SandboxExecutorClientError:
    reason_code = ""
    trace_id = ""
    try:
        payload = json.loads(error.read(8192).decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        payload = None
    detail = payload.get("detail") if isinstance(payload, dict) else None
    details = detail.get("details") if isinstance(detail, dict) else None
    if isinstance(details, dict):
        reason_code = _safe_error_token(details.get("reason"))
        trace_id = _safe_error_token(details.get("trace_id"), uppercase=False)
    if not reason_code and isinstance(detail, dict):
        reason_code = _safe_error_token(detail.get("code"))
    return SandboxExecutorClientError(
        message,
        reason_code=reason_code or f"HTTP_{error.code}",
        trace_id=trace_id,
    )


def _runtime_service_token() -> str:
    """Return the service credential accepted by Runtime internal APIs."""
    return str(
        os.environ.get("QWENPAW_SERVICE_TOKEN")
        or os.environ.get("RUNTIME_QWENPAW_SERVICE_TOKEN")
        or "",
    ).strip()


@dataclass(frozen=True)
class RuntimeSandboxExecutorClient:
    base_url: str
    token: str
    sandbox_context: dict[str, Any]
    tool_call_id: str
    tool_name: str
    tool_input: dict[str, Any]

    @classmethod
    def from_current_context(cls) -> "RuntimeSandboxExecutorClient | None":
        sandbox_context = get_current_runtime_sandbox_context()
        gateway = get_current_runtime_tool_gateway()
        execution = get_current_runtime_tool_execution()
        if sandbox_context is None and gateway is None and execution is None:
            return None
        if (
            not isinstance(sandbox_context, dict)
            or not isinstance(gateway, dict)
            or not isinstance(execution, dict)
        ):
            raise SandboxExecutorClientError("Runtime sandbox execution context is incomplete")
        base_url = resolve_runtime_base_url(gateway)
        token = _runtime_service_token()
        tool_call_id = str(execution.get("tool_call_id", "")).strip()
        tool_name = str(execution.get("tool_name", "")).strip()
        tool_input = execution.get("tool_input", {})
        if not token:
            raise SandboxExecutorClientError("Runtime service token is unavailable")
        if not base_url or not tool_call_id or not tool_name or not isinstance(tool_input, dict):
            raise SandboxExecutorClientError("Runtime sandbox execution context is incomplete")
        return cls(
            base_url=base_url,
            token=token,
            sandbox_context=dict(sandbox_context),
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            tool_input=dict(tool_input),
        )

    async def execute(self, operation: str) -> dict[str, Any]:
        payload = {
            "sandbox_context": dict(self.sandbox_context),
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "operation": str(operation),
            "arguments": dict(self.tool_input),
        }
        response = await asyncio.to_thread(self._post, payload)
        data = response.get("data") if isinstance(response, dict) else None
        if not isinstance(data, dict):
            raise SandboxExecutorClientError("Runtime sandbox returned an invalid response")
        return data

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = Request(
            f"{self.base_url}/runtime/internal/sandbox/execute",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=65) as response:  # noqa: S310 - fixed Runtime URL
                body = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise _runtime_http_error("Runtime sandbox operation failed", exc) from exc
        except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise SandboxExecutorClientError("Runtime sandbox operation failed") from exc
        if not isinstance(body, dict):
            raise SandboxExecutorClientError("Runtime sandbox returned an invalid response")
        return body
