"""Gateway errors retain trusted recovery facts without transport credentials."""
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bank_runtime.gateway.client import GatewayClient, GatewayError, _response_error
from bank_runtime.gateway.outbox import GatewayResultOutbox
from test_gateway_client import _config


def test_response_error_retains_structured_recovery_facts():
    error = _response_error({"detail": {"code": "ARTIFACT_VALIDATION_FAILED", "details": {
        "execution_status": "not_started", "retryable": True,
        "recovery_action": "correct_input", "remaining_attempts": 1,
        "token": "never-copy", "validation_hint": "use an array",
    }}}, "failed")
    assert error.failure_metadata == {
        "execution_status": "not_started", "retryable": True,
        "recovery_action": "correct_input", "remaining_attempts": 1,
    }


def test_renderer_exhaustion_has_explicit_nonretryable_recovery_status():
    error = _response_error({"code": "ARTIFACT_VALIDATION_FAILED", "details": {
        "reason": "presentation_layout_capacity", "retryable": False,
        "page_index": 2, "element": "title",
    }}, "failed")
    assert error.layout_failure == (2, "title")
    assert error.failure_metadata["retryable"] is False
    assert error.failure_metadata["recovery_action"] == "renderer_exhausted"
    assert error.failure_metadata["execution_status"] == "failed"


def test_recovery_facts_can_be_carried_by_error_envelope():
    error = _response_error({"code": "WORKER_UNAVAILABLE", "execution_status": "not_started",
                             "retryable": True, "remaining_attempts": 2}, "failed")
    assert error.failure_metadata == {"execution_status": "not_started", "retryable": True,
                                      "recovery_action": "retry", "remaining_attempts": 2}


def test_known_preflight_phase_keeps_input_correction_reachable():
    error = _response_error({"code": "ARTIFACT_VALIDATION_FAILED", "details": {
        "retryable": True, "recovery_action": "correct_input", "remaining_attempts": 1,
    }}, "failed", execution_status="not_started")
    assert error.execution_status == "not_started"
    assert error.failure_metadata["recovery_action"] == "correct_input"


@pytest.mark.asyncio
@pytest.mark.parametrize("phase, state", [("execute", "execution_unknown"), ("preflight", "not_started")])
async def test_transport_timeout_never_becomes_permission_to_repeat_execution(tmp_path, monkeypatch, phase, state):
    class HttpClient:
        def __init__(self, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def post(self, *args, **kwargs):
            raise httpx.ReadTimeout("test timeout")
    monkeypatch.setattr(httpx, "AsyncClient", HttpClient)
    client = GatewayClient(_config(), outbox=GatewayResultOutbox(tmp_path))
    with pytest.raises(GatewayError) as caught:
        await client._post({"phase": phase})
    assert caught.value.failure_metadata["execution_status"] == state
    assert caught.value.failure_metadata["retryable"] is False


@pytest.mark.asyncio
async def test_invalid_execution_acknowledgement_is_unknown(tmp_path, monkeypatch):
    client = GatewayClient(_config(), outbox=GatewayResultOutbox(tmp_path))
    async def post(payload):
        return {"status": "success", "tool_call_id": "unrelated"}
    monkeypatch.setattr(client, "_post", post)
    with pytest.raises(GatewayError) as caught:
        await client.execute_runtime_tool({"tool_call_id": "call-a", "permit": {"payload": {"permit_id": "permit-a"}}}, "artifact_generate", {})
    assert caught.value.failure_metadata["execution_status"] == "execution_unknown"
    assert caught.value.failure_metadata["retryable"] is False
