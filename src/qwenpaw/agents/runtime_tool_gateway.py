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

from .runtime_worker_protocol import (
    PROTOCOL_VERSION as WORKER_PROTOCOL_VERSION,
    RuntimeWorkerProtocolError,
    tool_intent,
    validate_tool_permit,
)
from .runtime_tool_outbox import RuntimeToolOutbox


RESULT_CALLBACK_MAX_ATTEMPTS = 3
_PROTOCOL = "preflight_guard_result_v2"


class RuntimeToolGatewayError(RuntimeError):
    """A Gateway callback could not be completed safely."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "",
        violation_type: str = "",
        public_summary: str = "",
    ) -> None:
        super().__init__(message)
        self.code = str(code)
        self.violation_type = str(violation_type)
        self.public_summary = str(public_summary)


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
    task_scope_id: str
    capability_snapshot_hash: str
    worker_protocol_version: str
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
            task_scope_id=str(gateway.get("task_scope_id", "")).strip(),
            capability_snapshot_hash=str(
                gateway.get("capability_snapshot_hash", "")
            ).strip(),
            worker_protocol_version=str(
                gateway.get("worker_protocol_version", "")
            ).strip(),
            trace_id=str(request_context.get("trace_id", "")).strip(),
        )

    async def preflight(
        self,
        worker_tool_name: str,
        tool_input: Mapping[str, Any],
        *,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        idempotency_key = str(idempotency_key).strip() or str(uuid.uuid4())
        if self.worker_protocol_version != WORKER_PROTOCOL_VERSION:
            return await self._legacy_preflight(
                worker_tool_name,
                tool_input,
                idempotency_key,
            )
        await self._flush_pending_results()
        call_id = idempotency_key.removeprefix("qwenpaw:") or idempotency_key
        intent = tool_intent(
            task_scope_id=self.task_scope_id,
            trace_id=self.trace_id or f"trace:{self.task_id}",
            call_id=call_id,
            idempotency_key=idempotency_key,
            capability_snapshot_hash=self.capability_snapshot_hash,
            tool_id=str(worker_tool_name),
            action_type="execute",
            tool_input=tool_input,
        )
        response = await self._post(
            self._url,
            {
                "phase": "preflight",
                **self._scope_payload(),
                "worker_tool_name": str(worker_tool_name),
                "input": dict(tool_input),
                "idempotency_key": idempotency_key,
                "trace_id": self.trace_id,
                "protocol_version": intent["protocol_version"],
                "message_type": intent["message_type"],
                "task_scope_id": intent["task_scope_id"],
                "call_id": intent["call_id"],
                "capability_snapshot_hash": intent[
                    "capability_snapshot_hash"
                ],
                "input_hash": intent["payload"]["input_hash"],
                "action_type": intent["payload"]["action_type"],
            },
            self._headers,
        )
        if response.get("phase") == "deny":
            raise self._error_from_response(response, "Tool Gateway denied this tool call")
        if (
            response.get("phase") != "allow"
            or response.get("decision") != "allow"
            or response.get("status") != "allowed"
            or not response.get("tool_call_id")
        ):
            raise self._error_from_response(response, "Tool Gateway denied this tool call")
        permit = response.get("permit")
        if not isinstance(permit, Mapping):
            raise RuntimeToolGatewayError("Tool Gateway permit is missing")
        try:
            validate_tool_permit(
                permit,
                task_scope_id=self.task_scope_id,
                tool_id=str(worker_tool_name),
                tool_input=tool_input,
                capability_snapshot_hash=self.capability_snapshot_hash,
            )
        except RuntimeWorkerProtocolError as exc:
            raise RuntimeToolGatewayError("Tool Gateway permit is invalid") from exc
        return {
            **response,
            "permit": dict(permit),
            "idempotency_key": idempotency_key,
        }

    async def _legacy_preflight(
        self,
        worker_tool_name: str,
        tool_input: Mapping[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        response = await self._post(
            self._url,
            {
                "phase": "preflight",
                **self._scope_payload(),
                "worker_tool_name": str(worker_tool_name),
                "input": dict(tool_input),
                "idempotency_key": idempotency_key,
                "trace_id": self.trace_id,
            },
            self._headers,
        )
        if (
            response.get("phase") != "allow"
            or response.get("decision") != "allow"
            or response.get("status") != "allowed"
            or not response.get("tool_call_id")
        ):
            raise self._error_from_response(
                response,
                "Tool Gateway denied this tool call",
            )
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
        if self.worker_protocol_version == WORKER_PROTOCOL_VERSION:
            payload.update(
                {
                    "protocol_version": WORKER_PROTOCOL_VERSION,
                    "message_type": "tool.result",
                    "task_scope_id": self.task_scope_id,
                    "trace_id": self.trace_id or f"trace:{self.task_id}",
                    "call_id": str(tool_call_id),
                    "idempotency_key": f"result:{tool_call_id}",
                    "capability_snapshot_hash": self.capability_snapshot_hash,
                },
            )
        last_error: Exception | None = None
        for attempt in range(RESULT_CALLBACK_MAX_ATTEMPTS):
            try:
                response = await self._post(self._url, payload, self._headers)
                RuntimeToolOutbox().remove(self.task_id, str(tool_call_id))
                return response
            except Exception as exc:  # noqa: BLE001 - callback errors are deliberately normalized
                last_error = exc
                if attempt + 1 < RESULT_CALLBACK_MAX_ATTEMPTS:
                    await asyncio.sleep(0.05 * (attempt + 1))
        RuntimeToolOutbox().enqueue(
            task_id=self.task_id,
            tool_call_id=str(tool_call_id),
            status=str(status),
            duration_ms=duration_ms,
            error_code=error_code,
            protocol_payload={
                key: str(payload.get(key) or "")
                for key in (
                    "protocol_version",
                    "message_type",
                    "task_scope_id",
                    "trace_id",
                    "call_id",
                    "idempotency_key",
                    "capability_snapshot_hash",
                )
                if payload.get(key)
            },
        )
        return {
            "tool_call_id": str(tool_call_id),
            "status": "result_pending",
            "retry_scheduled": True,
            "error_type": type(last_error).__name__ if last_error else "",
        }

    async def report_guard(
        self,
        tool_call_id: str,
        guard_decision: str,
        permit: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if guard_decision not in {"allow", "block", "require_approval"}:
            raise RuntimeToolGatewayError("Tool Guard decision is invalid")
        permit_payload = (
            permit.get("payload", {})
            if isinstance(permit, Mapping)
            and isinstance(permit.get("payload"), Mapping)
            else {}
        )
        protocol_payload = {}
        if self.worker_protocol_version == WORKER_PROTOCOL_VERSION:
            protocol_payload = {
                "protocol_version": WORKER_PROTOCOL_VERSION,
                "task_scope_id": self.task_scope_id,
                "capability_snapshot_hash": self.capability_snapshot_hash,
                "permit_id": str(permit_payload.get("permit_id") or ""),
                "permit_nonce": str(permit_payload.get("permit_nonce") or ""),
            }
        response = await self._post(
            self._url,
            {
                "phase": "guard",
                **self._scope_payload(),
                "tool_call_id": str(tool_call_id),
                "guard_decision": guard_decision,
                **protocol_payload,
            },
            self._headers,
        )
        expected_status = {
            "allow": "executing",
            "block": "cancelled",
            "require_approval": "pending_approval",
        }[guard_decision]
        if (
            response.get("tool_call_id") != str(tool_call_id)
            or response.get("guard_decision") != guard_decision
            or response.get("status") != expected_status
        ):
            raise RuntimeToolGatewayError(
                "Tool Gateway Guard acknowledgement is invalid",
            )
        return response

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

    async def _flush_pending_results(self) -> None:
        outbox = RuntimeToolOutbox()

        async def sender(record: dict[str, Any]) -> Any:
            protocol = record.get("protocol", {})
            if not isinstance(protocol, Mapping):
                protocol = {}
            payload = {
                "phase": "result",
                **self._scope_payload(),
                "tool_call_id": str(record.get("tool_call_id") or ""),
                "status": str(record.get("status") or ""),
                "duration_ms": max(int(record.get("duration_ms") or 0), 0),
                "error_code": str(record.get("error_code") or ""),
                **{str(key): value for key, value in protocol.items()},
            }
            response = await self._post(self._url, payload, self._headers)
            if response.get("tool_call_id") != payload["tool_call_id"]:
                raise RuntimeToolGatewayError(
                    "Tool Gateway outbox acknowledgement is invalid",
                )
            return response

        await outbox.flush(self.task_id, sender)

    async def _post(self, url: str, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        return await asyncio.to_thread(self._post_sync, url, payload, headers)

    @staticmethod
    def _error_from_response(payload: Mapping[str, Any], fallback: str) -> RuntimeToolGatewayError:
        detail = payload.get("detail") if isinstance(payload.get("detail"), Mapping) else payload
        details = detail.get("details") if isinstance(detail, Mapping) else {}
        details = details if isinstance(details, Mapping) else {}
        return RuntimeToolGatewayError(
            str(detail.get("message") or fallback) if isinstance(detail, Mapping) else fallback,
            code=str(detail.get("code") or "") if isinstance(detail, Mapping) else "",
            violation_type=str(details.get("violation_type") or ""),
            public_summary=(
                str(detail.get("public_summary") or "")
                if isinstance(detail, Mapping)
                else ""
            ),
        )

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
        except HTTPError as exc:
            try:
                error_body = json.loads(exc.read().decode("utf-8"))
            except (ValueError, OSError, UnicodeDecodeError):
                error_body = {}
            if isinstance(error_body, Mapping):
                raise RuntimeToolGatewayClient._error_from_response(
                    error_body,
                    "Tool Gateway request failed",
                ) from exc
            raise RuntimeToolGatewayError("Tool Gateway request failed") from exc
        except (URLError, ValueError, OSError) as exc:
            raise RuntimeToolGatewayError("Tool Gateway request failed") from exc
        if not isinstance(body, dict):
            raise RuntimeToolGatewayError("Tool Gateway response is invalid")
        return body
