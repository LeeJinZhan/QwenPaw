from datetime import datetime, timedelta, timezone

import pytest

from qwenpaw.agents.runtime_worker_protocol import (
    PROTOCOL_VERSION,
    RuntimeWorkerProtocolError,
    build_message,
    payload_hash,
    tool_intent,
    validate_message,
    validate_tool_permit,
)


def _envelope(message_type: str, payload=None):
    return build_message(
        message_type,
        task_scope_id="tscope-a",
        trace_id="trace-a",
        call_id="call-a",
        idempotency_key="idem-a",
        capability_snapshot_hash="sha256:cap-a",
        payload=payload,
    )


def test_all_runtime_worker_events_share_versioned_envelope():
    for message_type in (
        "task.start", "tool.intent", "tool.permit", "tool.result",
        "progress", "heartbeat", "cancel", "complete",
    ):
        message = _envelope(message_type)
        assert message["protocol_version"] == PROTOCOL_VERSION
        assert validate_message(message) == message


def test_intent_canonicalizes_input_and_scope():
    message = tool_intent(
        task_scope_id="tscope-a",
        trace_id="trace-a",
        call_id="call-a",
        idempotency_key="idem-a",
        capability_snapshot_hash="sha256:cap-a",
        tool_id="shell.exec",
        action_type="execute",
        tool_input={"b": 2, "a": 1},
    )
    assert message["payload"]["input_hash"] == payload_hash({"a": 1, "b": 2})
    with pytest.raises(RuntimeWorkerProtocolError):
        validate_message(message, expected_task_scope_id="tscope-b")


def test_worker_rejects_permit_replay_for_changed_tool_or_input():
    now = datetime(2026, 8, 3, tzinfo=timezone.utc)
    permit = _envelope(
        "tool.permit",
        {
            "permit_id": "permit-a",
            "permit_nonce": "once-a",
            "tool_id": "shell.exec",
            "input_hash": payload_hash({"command": "pwd"}),
            "expires_at": (now + timedelta(seconds=20)).isoformat(),
            "single_use": True,
        },
    )
    validate_tool_permit(
        permit,
        task_scope_id="tscope-a",
        tool_id="shell.exec",
        tool_input={"command": "pwd"},
        capability_snapshot_hash="sha256:cap-a",
        now=now,
    )
    with pytest.raises(RuntimeWorkerProtocolError):
        validate_tool_permit(
            permit,
            task_scope_id="tscope-a",
            tool_id="shell.exec",
            tool_input={"command": "id"},
            capability_snapshot_hash="sha256:cap-a",
            now=now,
        )
