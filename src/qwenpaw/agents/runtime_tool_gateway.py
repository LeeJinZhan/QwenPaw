# -*- coding: utf-8 -*-
"""Runtime Tool Gateway callbacks for Runtime-managed QwenPaw tasks."""
from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


RESULT_CALLBACK_MAX_ATTEMPTS = 3
_PROTOCOL = "preflight_result_v1"


class RuntimeToolGatewayError(RuntimeError):
    """A Gateway callback could not be completed safely."""


@dataclass(frozen=True)
class RuntimeToolGatewayClient:
    """Small task-scoped client that never records tool inputs or outputs."""

    base_url: str
    endpoint: str
    token: str
    task_id: str
    session_id: str
    tool_session_id: str
    policy_snapshot_id: str
    trace_id: str

    @classmethod
    def from_request_context(cls, request_context: Mapping[str, Any]) -> "RuntimeToolGatewayClient | None":
        gateway = request_context.get("runtime_tool_gateway")
        if not isinstance(gateway, Mapping) or gateway.get("protocol") != _PROTOCOL:
            return None
        required = (
            "base_url",
            "endpoint",
            "token",
            "task_id",
            "session_id",
            "tool_session_id",
            "policy_snapshot_id",
        )
        values = {key: str(gateway.get(key, "")).strip() for key in required}
        missing = [key for key, value in values.items() if not value]
        if missing:
            raise RuntimeToolGatewayError("Runtime Tool Gateway configuration is incomplete")
        return cls(
            **values,
            trace_id=str(request_context.get("trace_id", "")).strip(),
        )

    async def preflight(self, tool_id: str, tool_input: Mapping[str, Any]) -> dict[str, Any]:
        idempotency_key = str(uuid.uuid4())
        response = await self._post(
            self._url,
            {
                "phase": "preflight",
                **self._scope_payload(),
                "tool_id": str(tool_id),
                "input": dict(tool_input),
                "idempotency_key": idempotency_key,
                "trace_id": self.trace_id,
            },
            self._headers,
        )
        if response.get("decision") != "allow" or response.get("status") != "allowed" or not response.get("tool_call_id"):
            raise RuntimeToolGatewayError("Tool Gateway denied this tool call")
        return {**response, "idempotency_key": idempotency_key}

    async def report_result(
        self,
        tool_call_id: str,
        status: str,
        duration_ms: int,
        error_code: str = "",
    ) -> dict[str, Any]:
        payload = {
            "phase": "result",
            **self._scope_payload(),
            "tool_call_id": str(tool_call_id),
            "status": str(status),
            "duration_ms": max(int(duration_ms), 0),
            "error_code": str(error_code),
        }
        last_error: Exception | None = None
        for attempt in range(RESULT_CALLBACK_MAX_ATTEMPTS):
            try:
                return await self._post(self._url, payload, self._headers)
            except Exception as exc:  # noqa: BLE001 - callback errors are deliberately normalized
                last_error = exc
                if attempt + 1 < RESULT_CALLBACK_MAX_ATTEMPTS:
                    await asyncio.sleep(0.05 * (attempt + 1))
        raise RuntimeToolGatewayError("Tool Gateway audit writeback failed") from last_error

    @property
    def _url(self) -> str:
        return f"{self.base_url.rstrip('/')}/{self.endpoint.lstrip('/')}"

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def _scope_payload(self) -> dict[str, str]:
        return {
            "task_id": self.task_id,
            "session_id": self.session_id,
            "tool_session_id": self.tool_session_id,
            "policy_snapshot_id": self.policy_snapshot_id,
        }

    async def _post(self, url: str, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        return await asyncio.to_thread(self._post_sync, url, payload, headers)

    @staticmethod
    def _post_sync(url: str, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        request = Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={**headers, "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=10) as response:  # noqa: S310 - URL is Runtime-supplied task config
                body = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, ValueError, OSError) as exc:
            raise RuntimeToolGatewayError("Tool Gateway request failed") from exc
        if not isinstance(body, dict):
            raise RuntimeToolGatewayError("Tool Gateway response is invalid")
        return body
