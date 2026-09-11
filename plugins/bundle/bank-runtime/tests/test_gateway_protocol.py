from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from bank_runtime.gateway.protocol import (
    GatewayProtocolError,
    canonical_payload_hash,
    validate_tool_permit,
)


def _permit(tool_input: dict) -> dict:
    return {
        "protocol_version": "runtime-worker/v1",
        "message_type": "tool.permit",
        "task_scope_id": "scope_001",
        "trace_id": "trace_001",
        "call_id": "call_001",
        "idempotency_key": "qwenpaw:call_001",
        "capability_snapshot_hash": "sha256:capability",
        "payload": {
            "permit_id": "permit_001",
            "permit_nonce": "nonce-once",
            "task_id": "task_001",
            "agent_id": "bank-assistant",
            "tool_id": "policy_search",
            "input_hash": canonical_payload_hash(tool_input),
            "expires_at": (
                datetime.now(timezone.utc) + timedelta(seconds=30)
            ).isoformat(),
            "single_use": True,
        },
    }


def test_canonical_hash_is_order_independent_and_rejects_nan() -> None:
    assert canonical_payload_hash({"b": 2, "a": 1}) == canonical_payload_hash(
        {"a": 1, "b": 2}
    )
    with pytest.raises(GatewayProtocolError):
        canonical_payload_hash({"invalid": float("nan")})


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("task_id", "task_other"),
        ("agent_id", "agent_other"),
        ("tool_id", "tool_other"),
        ("input_hash", "sha256:other"),
        ("single_use", False),
    ],
)
def test_permit_binds_task_agent_tool_input_and_single_use(
    field: str,
    replacement: object,
) -> None:
    tool_input = {"query": "制度"}
    permit = _permit(tool_input)
    permit["payload"][field] = replacement

    with pytest.raises(GatewayProtocolError):
        validate_tool_permit(
            permit,
            task_scope_id="scope_001",
            task_id="task_001",
            agent_id="bank-assistant",
            tool_id="policy_search",
            tool_input=tool_input,
            capability_snapshot_hash="sha256:capability",
        )


def test_permit_rejects_expiry_and_scope_mismatch() -> None:
    tool_input = {"query": "制度"}
    permit = _permit(tool_input)
    permit["payload"]["expires_at"] = "2000-01-01T00:00:00+00:00"

    with pytest.raises(GatewayProtocolError):
        validate_tool_permit(
            permit,
            task_scope_id="scope_other",
            task_id="task_001",
            agent_id="bank-assistant",
            tool_id="policy_search",
            tool_input=tool_input,
            capability_snapshot_hash="sha256:capability",
        )
