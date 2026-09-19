"""Strict worker-side validation for Runtime-issued tool permits."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Mapping

PROTOCOL_VERSION = "runtime-worker/v1"


class GatewayProtocolError(ValueError):
    """A Runtime Tool Gateway message is malformed or out of scope."""


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
        raise GatewayProtocolError("payload is not canonicalizable") from exc


def canonical_payload_hash(value: Any) -> str:
    digest = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def validate_tool_permit(
    permit: Mapping[str, Any],
    *,
    task_scope_id: str,
    task_id: str,
    agent_id: str,
    tool_id: str,
    tool_input: Mapping[str, Any],
    capability_snapshot_hash: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    if not isinstance(permit, Mapping):
        raise GatewayProtocolError("permit must be an object")
    result = dict(permit)
    expected_envelope = {
        "protocol_version": PROTOCOL_VERSION,
        "message_type": "tool.permit",
        "task_scope_id": task_scope_id,
        "capability_snapshot_hash": capability_snapshot_hash,
    }
    for field, expected in expected_envelope.items():
        if result.get(field) != expected:
            raise GatewayProtocolError(f"permit {field} mismatch")
    for field in ("trace_id", "call_id", "idempotency_key"):
        if not isinstance(result.get(field), str) or not result[field].strip():
            raise GatewayProtocolError(f"permit {field} is missing")
    payload = result.get("payload")
    if not isinstance(payload, Mapping):
        raise GatewayProtocolError("permit payload is missing")
    payload = dict(payload)
    expected_payload = {
        "task_id": task_id,
        "agent_id": agent_id,
        "tool_id": tool_id,
        "input_hash": canonical_payload_hash(tool_input),
    }
    for field, expected in expected_payload.items():
        if payload.get(field) != expected:
            raise GatewayProtocolError(f"permit {field} mismatch")
    if payload.get("single_use") is not True:
        raise GatewayProtocolError("permit is not single use")
    for field in ("permit_id", "permit_nonce", "expires_at"):
        if not isinstance(payload.get(field), str) or not payload[field].strip():
            raise GatewayProtocolError(f"permit {field} is missing")
    try:
        expires_at = datetime.fromisoformat(payload["expires_at"])
    except ValueError as exc:
        raise GatewayProtocolError("permit expiry is invalid") from exc
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    if expires_at <= current:
        raise GatewayProtocolError("permit expired")
    result["payload"] = payload
    return result


__all__ = [
    "GatewayProtocolError",
    "PROTOCOL_VERSION",
    "canonical_json",
    "canonical_payload_hash",
    "validate_tool_permit",
]
