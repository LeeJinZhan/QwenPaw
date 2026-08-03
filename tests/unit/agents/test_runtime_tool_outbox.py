from __future__ import annotations

import json

import pytest

from qwenpaw.agents.runtime_tool_outbox import RuntimeToolOutbox


def test_outbox_persists_only_safe_result_metadata(tmp_path):
    outbox = RuntimeToolOutbox(tmp_path)
    outbox.enqueue(
        task_id="task-a",
        tool_call_id="call-a",
        status="completed",
        duration_ms=42,
        protocol_payload={"task_scope_id": "tscope-a"},
    )

    reloaded = RuntimeToolOutbox(tmp_path).pending("task-a")

    assert len(reloaded) == 1
    rendered = json.dumps(reloaded)
    assert "output" not in rendered
    assert "result" not in rendered
    assert "input" not in rendered


@pytest.mark.asyncio
async def test_flush_retries_callback_without_reexecuting_tool(tmp_path):
    outbox = RuntimeToolOutbox(tmp_path)
    outbox.enqueue(
        task_id="task-a",
        tool_call_id="call-a",
        status="execution_unknown",
        duration_ms=10,
    )
    sends = 0

    async def failing_sender(_record):
        nonlocal sends
        sends += 1
        raise RuntimeError("offline")

    assert await outbox.flush("task-a", failing_sender) == {
        "delivered": 0,
        "pending": 1,
    }
    assert sends == 1
    assert outbox.pending("task-a")[0]["attempts"] == 1

    async def successful_sender(record):
        nonlocal sends
        sends += 1
        assert record["tool_call_id"] == "call-a"

    assert await outbox.flush("task-a", successful_sender) == {
        "delivered": 1,
        "pending": 0,
    }
    assert sends == 2
    assert outbox.pending("task-a") == []


def test_outbox_distinguishes_cancel_and_unknown_execution_states(tmp_path):
    outbox = RuntimeToolOutbox(tmp_path)
    for index, status in enumerate(
        ("not_started_cancelled", "execution_interrupted", "execution_unknown"),
    ):
        outbox.enqueue(
            task_id="task-a",
            tool_call_id=f"call-{index}",
            status=status,
            duration_ms=0,
        )
    assert {row["status"] for row in outbox.pending("task-a")} == {
        "not_started_cancelled",
        "execution_interrupted",
        "execution_unknown",
    }
