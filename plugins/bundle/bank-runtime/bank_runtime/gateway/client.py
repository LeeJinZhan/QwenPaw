"""Task-scoped Runtime Tool Gateway client with fixed transport policy."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlsplit
import uuid

import httpx

from .outbox import GatewayResultOutbox
from .protocol import (
    PROTOCOL_VERSION,
    GatewayProtocolError,
    canonical_payload_hash,
    validate_tool_permit,
)

_GATEWAY_PROTOCOL = "preflight_guard_result_v2"
_ENDPOINT = "/runtime/v1/tool-calls"
_FORBIDDEN_CONFIG_FIELDS = frozenset(
    {"allowed_tools", "disabled_tools", "worker_tool_bindings"}
)
_RESULT_ATTEMPTS = 3


class GatewayError(RuntimeError):
    """Gateway mediation failed safely."""

    def __init__(self, message: str, *, code: str = "") -> None:
        super().__init__(message)
        self.code = str(code or "")


@dataclass(frozen=True)
class GatewayConfig:
    base_url: str
    endpoint: str
    token: str
    task_id: str
    session_id: str
    tool_session_id: str
    policy_snapshot_id: str
    task_scope_id: str
    capability_snapshot_hash: str
    worker_protocol_version: str
    trace_id: str
    agent_id: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "GatewayConfig":
        if not isinstance(value, Mapping):
            raise GatewayError("Runtime Tool Gateway configuration is missing")
        polluted = _FORBIDDEN_CONFIG_FIELDS & set(value)
        if polluted:
            raise GatewayError("Runtime Tool Gateway cannot define tool visibility")
        if value.get("protocol") != _GATEWAY_PROTOCOL:
            raise GatewayError("Runtime Tool Gateway protocol is unsupported")
        if value.get("worker_protocol_version") != PROTOCOL_VERSION:
            raise GatewayError("Runtime-Worker protocol is unsupported")
        endpoint = str(value.get("endpoint") or "").strip()
        if endpoint != _ENDPOINT:
            raise GatewayError("Runtime Tool Gateway endpoint is not approved")
        base_url = str(value.get("base_url") or "").strip()
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise GatewayError("Runtime Tool Gateway base URL is invalid")
        required = {
            "base_url": base_url.rstrip("/"),
            "endpoint": endpoint,
            "token": str(value.get("token") or "").strip(),
            "task_id": str(value.get("task_id") or "").strip(),
            "session_id": str(value.get("session_id") or "").strip(),
            "tool_session_id": str(value.get("tool_session_id") or "").strip(),
            "policy_snapshot_id": str(value.get("policy_snapshot_id") or "").strip(),
            "task_scope_id": str(value.get("task_scope_id") or "").strip(),
            "capability_snapshot_hash": str(
                value.get("capability_snapshot_hash") or ""
            ).strip(),
            "worker_protocol_version": PROTOCOL_VERSION,
            "trace_id": str(value.get("trace_id") or "").strip(),
            "agent_id": str(
                value.get("worker_agent_id") or value.get("agent_id") or ""
            ).strip(),
        }
        missing = [key for key, item in required.items() if not item]
        if missing:
            raise GatewayError("Runtime Tool Gateway configuration is incomplete")
        return cls(**required)


class GatewayClient:
    """Posts only fixed Runtime callbacks and never persists tool content."""

    def __init__(
        self,
        config: GatewayConfig,
        *,
        outbox: GatewayResultOutbox | None = None,
    ) -> None:
        self.config = config
        self.outbox = outbox or GatewayResultOutbox()

    async def preflight(
        self,
        tool_name: str,
        tool_input: Mapping[str, Any],
        *,
        call_id: str = "",
    ) -> dict[str, Any]:
        call_id = str(call_id or "").strip() or f"call_{uuid.uuid4().hex}"
        idempotency_key = f"qwenpaw:{call_id}"
        await self._flush_pending_results()
        payload = {
            "phase": "preflight",
            **self._scope_payload(),
            "worker_tool_name": str(tool_name),
            "input": dict(tool_input),
            "protocol_version": PROTOCOL_VERSION,
            "message_type": "tool.intent",
            "task_scope_id": self.config.task_scope_id,
            "trace_id": self.config.trace_id,
            "call_id": call_id,
            "idempotency_key": idempotency_key,
            "capability_snapshot_hash": self.config.capability_snapshot_hash,
            "input_hash": canonical_payload_hash(tool_input),
            "action_type": "execute",
        }
        response = await self._post(payload)
        if (
            response.get("phase") != "allow"
            or response.get("decision") != "allow"
            or response.get("status") != "allowed"
            or not response.get("tool_call_id")
        ):
            raise _response_error(response, "Runtime denied this tool call")
        permit = response.get("permit")
        try:
            validated = validate_tool_permit(
                permit,
                task_scope_id=self.config.task_scope_id,
                task_id=self.config.task_id,
                agent_id=self.config.agent_id,
                tool_id=str(tool_name),
                tool_input=tool_input,
                capability_snapshot_hash=self.config.capability_snapshot_hash,
            )
        except GatewayProtocolError as exc:
            raise GatewayError("Runtime Tool Gateway permit is invalid") from exc
        return {
            **response,
            "permit": validated,
            "call_id": call_id,
            "idempotency_key": idempotency_key,
        }

    async def report_guard(
        self,
        preflight: Mapping[str, Any],
        decision: str,
    ) -> dict[str, Any]:
        if decision not in {"allow", "block", "require_approval"}:
            raise GatewayError("Tool Guard decision is invalid")
        permit = preflight.get("permit")
        permit_payload = permit.get("payload") if isinstance(permit, Mapping) else None
        if not isinstance(permit_payload, Mapping):
            raise GatewayError("Runtime Tool Gateway permit is missing")
        payload = {
            "phase": "guard",
            **self._scope_payload(),
            "tool_call_id": str(preflight.get("tool_call_id") or ""),
            "guard_decision": decision,
            "protocol_version": PROTOCOL_VERSION,
            "task_scope_id": self.config.task_scope_id,
            "capability_snapshot_hash": self.config.capability_snapshot_hash,
            "permit_id": str(permit_payload.get("permit_id") or ""),
            "permit_nonce": str(permit_payload.get("permit_nonce") or ""),
        }
        response = await self._post(payload)
        expected = {
            "allow": "executing",
            "block": "cancelled",
            "require_approval": "pending_approval",
        }[decision]
        if (
            response.get("tool_call_id") != payload["tool_call_id"]
            or response.get("guard_decision") != decision
            or response.get("status") != expected
        ):
            raise GatewayError("Runtime Tool Guard acknowledgement is invalid")
        return response

    async def execute_runtime_tool(
        self,
        preflight: Mapping[str, Any],
        tool_name: str,
        tool_input: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Execute one preflighted Runtime-native tool using the consumed permit."""
        permit = preflight.get("permit")
        permit_payload = permit.get("payload") if isinstance(permit, Mapping) else None
        if not isinstance(permit_payload, Mapping):
            raise GatewayError("Runtime Tool Gateway permit is missing")
        payload = {
            "phase": "execute",
            **self._scope_payload(),
            "worker_tool_name": str(tool_name),
            "input": dict(tool_input),
            "tool_call_id": str(preflight.get("tool_call_id") or ""),
            "permit_id": str(permit_payload.get("permit_id") or ""),
            "protocol_version": PROTOCOL_VERSION,
            "message_type": "tool.intent",
            "task_scope_id": self.config.task_scope_id,
            "trace_id": self.config.trace_id,
            "call_id": str(preflight.get("call_id") or ""),
            "idempotency_key": str(preflight.get("idempotency_key") or ""),
            "capability_snapshot_hash": self.config.capability_snapshot_hash,
            "input_hash": canonical_payload_hash(tool_input),
            "action_type": "execute",
        }
        response = await self._post(payload)
        if response.get("tool_call_id") != payload["tool_call_id"]:
            raise GatewayError("Runtime tool execution acknowledgement is invalid")
        if response.get("status") not in {"success", "failed", "blocked"}:
            raise GatewayError("Runtime tool execution result is invalid")
        return response

    async def report_result(
        self,
        tool_call_id: str,
        status: str,
        duration_ms: int,
        error_code: str = "",
    ) -> dict[str, Any]:
        protocol = {
            "protocol_version": PROTOCOL_VERSION,
            "message_type": "tool.result",
            "task_scope_id": self.config.task_scope_id,
            "trace_id": self.config.trace_id,
            "call_id": str(tool_call_id),
            "idempotency_key": f"result:{tool_call_id}",
            "capability_snapshot_hash": self.config.capability_snapshot_hash,
        }
        payload = {
            "phase": "result",
            **self._scope_payload(),
            "tool_call_id": str(tool_call_id),
            "status": str(status),
            "duration_ms": max(int(duration_ms), 0),
            "error_code": str(error_code or "")[:128],
            **protocol,
        }
        last_error: Exception | None = None
        for attempt in range(_RESULT_ATTEMPTS):
            try:
                response = await self._post(payload)
                if response.get("tool_call_id") != str(tool_call_id):
                    raise GatewayError(
                        "Runtime Tool Gateway result acknowledgement is invalid"
                    )
                self.outbox.remove(self.config.task_id, str(tool_call_id))
                return response
            except Exception as exc:  # result callback is retried, execution is not
                last_error = exc
                if attempt + 1 < _RESULT_ATTEMPTS:
                    await asyncio.sleep(0.05 * (attempt + 1))
        self.outbox.enqueue(
            task_id=self.config.task_id,
            tool_call_id=str(tool_call_id),
            status=str(status),
            duration_ms=duration_ms,
            error_code=error_code,
            protocol=protocol,
        )
        return {
            "tool_call_id": str(tool_call_id),
            "status": "result_pending",
            "retry_scheduled": True,
            "error_type": type(last_error).__name__ if last_error else "",
        }

    def _scope_payload(self) -> dict[str, str]:
        return {
            "task_id": self.config.task_id,
            "session_id": self.config.session_id,
            "tool_session_id": self.config.tool_session_id,
            "policy_snapshot_id": self.config.policy_snapshot_id,
            "worker_agent_id": self.config.agent_id,
        }

    @property
    def _url(self) -> str:
        return f"{self.config.base_url}{self.config.endpoint}"

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(
                timeout=10,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                response = await client.post(
                    self._url,
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {self.config.token}",
                        "Content-Type": "application/json",
                    },
                )
        except httpx.HTTPError as exc:
            raise GatewayError("Runtime Tool Gateway request failed") from exc
        try:
            body = response.json()
        except ValueError as exc:
            raise GatewayError("Runtime Tool Gateway response is invalid") from exc
        if not isinstance(body, dict):
            raise GatewayError("Runtime Tool Gateway response is invalid")
        if response.status_code >= 400:
            raise _response_error(body, "Runtime Tool Gateway request failed")
        return body

    async def _flush_pending_results(self) -> None:
        async def sender(record: dict[str, Any]) -> None:
            protocol = record.get("protocol")
            if not isinstance(protocol, Mapping):
                protocol = {}
            response = await self._post(
                {
                    "phase": "result",
                    **self._scope_payload(),
                    "tool_call_id": str(record.get("tool_call_id") or ""),
                    "status": str(record.get("status") or ""),
                    "duration_ms": max(int(record.get("duration_ms") or 0), 0),
                    "error_code": str(record.get("error_code") or "")[:128],
                    **dict(protocol),
                }
            )
            if response.get("tool_call_id") != record.get("tool_call_id"):
                raise GatewayError(
                    "Runtime Tool Gateway outbox acknowledgement is invalid"
                )

        await self.outbox.flush(self.config.task_id, sender)


def _response_error(payload: Mapping[str, Any], fallback: str) -> GatewayError:
    detail = payload.get("detail")
    if not isinstance(detail, Mapping):
        detail = payload
    return GatewayError(
        str(detail.get("message") or fallback),
        code=str(detail.get("code") or ""),
    )


__all__ = ["GatewayClient", "GatewayConfig", "GatewayError"]
