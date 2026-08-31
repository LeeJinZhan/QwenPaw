from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from bank_runtime.gateway.client import GatewayClient, GatewayConfig, GatewayError
from bank_runtime.gateway.outbox import GatewayResultOutbox
from bank_runtime.gateway.protocol import canonical_payload_hash


def _config() -> GatewayConfig:
    return GatewayConfig.from_mapping(
        {
            "protocol": "preflight_guard_result_v2",
            "base_url": "http://127.0.0.1:8765",
            "endpoint": "/runtime/v1/tool-calls",
            "token": "worker-secret",
            "task_id": "task_001",
            "session_id": "session_001",
            "tool_session_id": "wts_001",
            "policy_snapshot_id": "policy_001",
            "task_scope_id": "scope_001",
            "capability_snapshot_hash": "sha256:capability",
            "worker_protocol_version": "runtime-worker/v1",
            "trace_id": "trace_001",
            "worker_agent_id": "bank-assistant",
        }
    )


def _permit(tool_input, *, agent_id="bank-assistant", tool_id="policy_search") -> dict:
    return {
        "protocol_version": "runtime-worker/v1",
        "message_type": "tool.permit",
        "task_scope_id": "scope_001",
        "trace_id": "trace_001",
        "call_id": "model_call_001",
        "idempotency_key": "qwenpaw:model_call_001",
        "capability_snapshot_hash": "sha256:capability",
        "payload": {
            "permit_id": "permit_001",
            "permit_nonce": "nonce-once",
            "task_id": "task_001",
            "agent_id": agent_id,
            "tool_id": tool_id,
            "input_hash": canonical_payload_hash(tool_input),
            "expires_at": (
                datetime.now(timezone.utc) + timedelta(seconds=30)
            ).isoformat(),
            "single_use": True,
        },
    }


@pytest.mark.asyncio
async def test_client_keeps_one_correlation_across_preflight_guard_result(
    tmp_path,
    monkeypatch,
) -> None:
    client = GatewayClient(_config(), outbox=GatewayResultOutbox(tmp_path))
    requests = []
    tool_input = {"query": "制度"}

    async def post(payload):
        requests.append(payload)
        if payload["phase"] == "preflight":
            return {
                "phase": "allow",
                "decision": "allow",
                "status": "allowed",
                "tool_call_id": "runtime_call_001",
                "permit": _permit(tool_input),
            }
        if payload["phase"] == "guard":
            return {
                "tool_call_id": "runtime_call_001",
                "guard_decision": "allow",
                "status": "executing",
            }
        return {"tool_call_id": "runtime_call_001", "status": "completed"}

    monkeypatch.setattr(client, "_post", post)
    preflight = await client.preflight(
        "policy_search",
        tool_input,
        call_id="model_call_001",
    )
    await client.report_guard(preflight, "allow")
    await client.report_result("runtime_call_001", "completed", 9)

    assert [request["phase"] for request in requests] == [
        "preflight",
        "guard",
        "result",
    ]
    assert requests[0]["call_id"] == "model_call_001"
    assert requests[0]["input_hash"] == canonical_payload_hash(tool_input)
    assert requests[1]["permit_id"] == "permit_001"
    assert requests[2]["call_id"] == "runtime_call_001"
    assert all(request["worker_agent_id"] == "bank-assistant" for request in requests)
    assert not list(tmp_path.glob("*.json"))


@pytest.mark.asyncio
async def test_client_executes_runtime_native_tool_with_same_permit_and_input(
    monkeypatch,
) -> None:
    client = GatewayClient(_config())
    tool_input = {"artifact_type": "docx", "title": "纪要", "content": {}}
    requests = []

    async def post(payload):
        requests.append(payload)
        return {
            "tool_call_id": "runtime_call_001",
            "status": "success",
            "result": {"artifact_job_id": "artifact_job_001"},
        }

    monkeypatch.setattr(client, "_post", post)
    preflight = {
        "tool_call_id": "runtime_call_001",
        "call_id": "model_call_001",
        "idempotency_key": "qwenpaw:model_call_001",
        "permit": _permit(tool_input, tool_id="artifact_generate"),
    }

    result = await client.execute_runtime_tool(
        preflight,
        "artifact_generate",
        tool_input,
    )

    assert result["result"]["artifact_job_id"] == "artifact_job_001"
    assert requests == [
        {
            "phase": "execute",
            "task_id": "task_001",
            "session_id": "session_001",
            "tool_session_id": "wts_001",
            "policy_snapshot_id": "policy_001",
            "worker_agent_id": "bank-assistant",
            "worker_tool_name": "artifact_generate",
            "input": tool_input,
            "tool_call_id": "runtime_call_001",
            "permit_id": "permit_001",
            "protocol_version": "runtime-worker/v1",
            "message_type": "tool.intent",
            "task_scope_id": "scope_001",
            "trace_id": "trace_001",
            "call_id": "model_call_001",
            "idempotency_key": "qwenpaw:model_call_001",
            "capability_snapshot_hash": "sha256:capability",
            "input_hash": canonical_payload_hash(tool_input),
            "action_type": "execute",
        }
    ]


@pytest.mark.asyncio
async def test_client_rejects_runtime_permit_with_wrong_agent(monkeypatch) -> None:
    client = GatewayClient(_config())
    tool_input = {"query": "制度"}

    async def post(_payload):
        return {
            "phase": "allow",
            "decision": "allow",
            "status": "allowed",
            "tool_call_id": "runtime_call_001",
            "permit": _permit(tool_input, agent_id="other-agent"),
        }

    monkeypatch.setattr(client, "_post", post)
    with pytest.raises(GatewayError):
        await client.preflight(
            "policy_search",
            tool_input,
            call_id="model_call_001",
        )
