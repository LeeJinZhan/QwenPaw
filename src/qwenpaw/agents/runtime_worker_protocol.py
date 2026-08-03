# -*- coding: utf-8 -*-
"""Worker-side validation for Runtime-issued task and tool messages."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Mapping


PROTOCOL_VERSION = "runtime-worker/v1"
MESSAGE_TYPES = frozenset(
    {
        "task.start", "tool.intent", "tool.permit", "tool.result",
        "progress", "heartbeat", "cancel", "complete",
    },
)


class RuntimeWorkerProtocolError(ValueError):
    """The Runtime message is malformed or inconsistent with this request."""


def canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise RuntimeWorkerProtocolError("payload is not canonicalizable") from exc


def payload_hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode()).hexdigest()


def build_message(
    message_type: str,
    *,
    task_scope_id: str,
    trace_id: str,
    call_id: str,
    idempotency_key: str,
    capability_snapshot_hash: str,
    payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return validate_message(
        {
            "protocol_version": PROTOCOL_VERSION,
            "message_type": str(message_type or "").strip(),
            "task_scope_id": str(task_scope_id or "").strip(),
            "trace_id": str(trace_id or "").strip(),
            "call_id": str(call_id or "").strip(),
            "idempotency_key": str(idempotency_key or "").strip(),
            "capability_snapshot_hash": str(capability_snapshot_hash or "").strip(),
            "payload": dict(payload or {}),
        },
    )


def validate_message(
    message: Mapping[str, Any],
    *,
    expected_type: str = "",
    expected_task_scope_id: str = "",
    expected_capability_snapshot_hash: str = "",
) -> dict[str, Any]:
    if not isinstance(message, Mapping):
        raise RuntimeWorkerProtocolError("message must be an object")
    result = dict(message)
    if result.get("protocol_version") != PROTOCOL_VERSION:
        raise RuntimeWorkerProtocolError("unsupported protocol version")
    if str(result.get("message_type") or "") not in MESSAGE_TYPES:
        raise RuntimeWorkerProtocolError("unsupported message type")
    if expected_type and result["message_type"] != expected_type:
        raise RuntimeWorkerProtocolError("message type mismatch")
    for field in (
        "task_scope_id", "trace_id", "call_id", "idempotency_key",
        "capability_snapshot_hash",
    ):
        if not isinstance(result.get(field), str) or not result[field].strip():
            raise RuntimeWorkerProtocolError(f"missing field: {field}")
        result[field] = result[field].strip()
    if expected_task_scope_id and result["task_scope_id"] != expected_task_scope_id:
        raise RuntimeWorkerProtocolError("task scope mismatch")
    if (
        expected_capability_snapshot_hash
        and result["capability_snapshot_hash"] != expected_capability_snapshot_hash
    ):
        raise RuntimeWorkerProtocolError("capability snapshot mismatch")
    if not isinstance(result.get("payload"), Mapping):
        raise RuntimeWorkerProtocolError("payload must be an object")
    result["payload"] = dict(result["payload"])
    return result


def tool_intent(
    *,
    task_scope_id: str,
    trace_id: str,
    call_id: str,
    idempotency_key: str,
    capability_snapshot_hash: str,
    tool_id: str,
    action_type: str,
    tool_input: Mapping[str, Any],
) -> dict[str, Any]:
    return build_message(
        "tool.intent",
        task_scope_id=task_scope_id,
        trace_id=trace_id,
        call_id=call_id,
        idempotency_key=idempotency_key,
        capability_snapshot_hash=capability_snapshot_hash,
        payload={
            "tool_id": str(tool_id or "").strip(),
            "action_type": str(action_type or "").strip(),
            "input": dict(tool_input),
            "input_hash": payload_hash(tool_input),
        },
    )


def validate_tool_permit(
    permit: Mapping[str, Any],
    *,
    task_scope_id: str,
    tool_id: str,
    tool_input: Mapping[str, Any],
    capability_snapshot_hash: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    result = validate_message(
        permit,
        expected_type="tool.permit",
        expected_task_scope_id=task_scope_id,
        expected_capability_snapshot_hash=capability_snapshot_hash,
    )
    payload = result["payload"]
    if payload.get("tool_id") != tool_id:
        raise RuntimeWorkerProtocolError("permit tool mismatch")
    if payload.get("input_hash") != payload_hash(tool_input):
        raise RuntimeWorkerProtocolError("permit input mismatch")
    if payload.get("single_use") is not True:
        raise RuntimeWorkerProtocolError("permit is not single use")
    if not all(str(payload.get(field) or "").strip() for field in ("permit_id", "permit_nonce", "expires_at")):
        raise RuntimeWorkerProtocolError("permit is incomplete")
    try:
        expires_at = datetime.fromisoformat(str(payload["expires_at"]))
    except ValueError as exc:
        raise RuntimeWorkerProtocolError("permit expiry is invalid") from exc
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    if expires_at <= current:
        raise RuntimeWorkerProtocolError("permit expired")
    return result
