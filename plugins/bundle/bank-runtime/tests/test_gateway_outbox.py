from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from bank_runtime.gateway.outbox import GatewayResultOutbox


@pytest.mark.asyncio
async def test_outbox_keeps_metadata_only_and_stops_after_retry_cap(tmp_path) -> None:
    outbox = GatewayResultOutbox(tmp_path, max_attempts=2)
    outbox.enqueue(
        task_id="task_001",
        tool_call_id="call_001",
        status="failed",
        duration_ms=12,
        error_code="TOOL_FAILED",
        protocol={"trace_id": "trace_001"},
    )

    payload = json.loads(next(tmp_path.glob("*.json")).read_text(encoding="utf-8"))
    assert set(payload) == {
        "attempts",
        "created_at_epoch",
        "duration_ms",
        "error_code",
        "protocol",
        "status",
        "task_id",
        "tool_call_id",
        "updated_at_epoch",
    }
    assert not ({"input", "output", "result", "token", "credential"} & set(payload))

    attempts = 0

    async def fail(_record):
        nonlocal attempts
        attempts += 1
        raise RuntimeError("offline")

    await outbox.flush("task_001", fail)
    await outbox.flush("task_001", fail)
    await outbox.flush("task_001", fail)
    assert attempts == 2
    assert outbox.pending("task_001")[0]["attempts"] == 2


def test_outbox_rejects_unknown_status_and_unsafe_identifier(tmp_path) -> None:
    outbox = GatewayResultOutbox(tmp_path)
    with pytest.raises(ValueError):
        outbox.enqueue(
            task_id="../escape",
            tool_call_id="call_001",
            status="completed",
            duration_ms=1,
        )
    with pytest.raises(ValueError):
        outbox.enqueue(
            task_id="task_001",
            tool_call_id="call_001",
            status="retry_execution",
            duration_ms=1,
        )
