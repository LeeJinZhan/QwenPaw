# -*- coding: utf-8 -*-
"""Tests for the Runtime Tool Gateway callback client."""

from __future__ import annotations

import io
import json
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError

import pytest

import qwenpaw.agents.runtime_tool_gateway as gateway_module
from qwenpaw.agents.runtime_tool_gateway import RuntimeToolGatewayClient, RuntimeToolGatewayError
from qwenpaw.agents.runtime_worker_protocol import build_message, payload_hash


def _context() -> dict:
    return {
        "runtime_tool_gateway": {
            "protocol": "preflight_guard_result_v2",
            "base_url": "http://runtime.example",
            "endpoint": "/runtime/v1/tool-calls",
            "token": "worker-token",
            "task_id": "task_001",
            "session_id": "sess_001",
            "tool_session_id": "wts_001",
            "policy_snapshot_id": "ps_001",
        },
        "trace_id": "trace_001",
    }


def _v1_context() -> dict:
    context = _context()
    context["runtime_tool_gateway"].update(
        worker_protocol_version="runtime-worker/v1",
        task_scope_id="tscope_001",
        capability_snapshot_hash="sha256:capabilities",
    )
    return context


@pytest.mark.asyncio
async def test_v1_preflight_requires_exact_single_use_permit(monkeypatch):
    client = RuntimeToolGatewayClient.from_request_context(_v1_context())
    requests: list[dict] = []

    async def fake_post(_self, _url, payload, _headers):
        requests.append(payload)
        permit = build_message(
            "tool.permit",
            task_scope_id="tscope_001",
            trace_id="trace_001",
            call_id="call_001",
            idempotency_key="qwenpaw:call_001",
            capability_snapshot_hash="sha256:capabilities",
            payload={
                "permit_id": "tgrant_001",
                "permit_nonce": "nonce-once",
                "tool_id": "execute_shell_command",
                "input_hash": payload_hash({"command": "pwd"}),
                "expires_at": (
                    datetime.now(timezone.utc) + timedelta(seconds=30)
                ).isoformat(),
                "single_use": True,
            },
        )
        return {
            "phase": "allow",
            "tool_call_id": "tool_001",
            "decision": "allow",
            "status": "allowed",
            "permit": permit,
        }

    monkeypatch.setattr(RuntimeToolGatewayClient, "_post", fake_post)

    result = await client.preflight(
        "execute_shell_command",
        {"command": "pwd"},
        idempotency_key="qwenpaw:call_001",
    )

    assert result["permit"]["payload"]["permit_id"] == "tgrant_001"
    assert requests[0]["protocol_version"] == "runtime-worker/v1"
    assert requests[0]["task_scope_id"] == "tscope_001"
    assert requests[0]["input_hash"] == payload_hash({"command": "pwd"})


@pytest.mark.asyncio
async def test_preflight_uses_task_gateway_context_and_returns_allow(monkeypatch):
    client = RuntimeToolGatewayClient.from_request_context(_context())
    requests: list[tuple[str, dict, dict]] = []

    async def fake_post(_self, url, payload, headers):
        requests.append((url, payload, headers))
        return {"phase": "allow", "tool_call_id": "tool_001", "decision": "allow", "status": "allowed", "trace_id": "trace_001"}

    monkeypatch.setattr(RuntimeToolGatewayClient, "_post", fake_post)

    result = await client.preflight("mcp.policy.search", {"query": "制度"})

    assert result["tool_call_id"] == "tool_001"
    assert requests == [
        (
            "http://runtime.example/runtime/v1/tool-calls",
            {
                "phase": "preflight",
                "task_id": "task_001",
                "session_id": "sess_001",
                "tool_session_id": "wts_001",
                "policy_snapshot_id": "ps_001",
                "worker_tool_name": "mcp.policy.search",
                "input": {"query": "制度"},
                "idempotency_key": result["idempotency_key"],
                "trace_id": "trace_001",
            },
            {"Authorization": "Bearer worker-token"},
        )
    ]


@pytest.mark.asyncio
async def test_preflight_preserves_structured_runtime_denial(monkeypatch):
    client = RuntimeToolGatewayClient.from_request_context(_context())

    async def fake_post(_self, _url, _payload, _headers):
        return {
            "phase": "deny",
            "code": "POLICY_BLOCKED",
            "message": "internal policy detail",
            "details": {"violation_type": "assistant_tool_not_allowed"},
            "public_summary": "当前助手未授权使用该工具，请尝试其他方式完成请求。",
        }

    monkeypatch.setattr(RuntimeToolGatewayClient, "_post", fake_post)

    with pytest.raises(RuntimeToolGatewayError) as raised:
        await client.preflight("execute_shell_command", {"command": "pwd"})

    assert raised.value.code == "POLICY_BLOCKED"
    assert raised.value.violation_type == "assistant_tool_not_allowed"
    assert raised.value.public_summary == "当前助手未授权使用该工具，请尝试其他方式完成请求。"


def test_http_error_preserves_safe_runtime_denial(monkeypatch):
    response_body = json.dumps(
        {
            "detail": {
                "code": "POLICY_BLOCKED",
                "message": "internal policy detail",
                "details": {"violation_type": "assistant_tool_not_allowed"},
                "public_summary": "当前助手未授权使用该工具，请尝试其他方式完成请求。",
            },
        },
        ensure_ascii=False,
    ).encode("utf-8")

    def denied_urlopen(request, timeout):
        raise HTTPError(request.full_url, 403, "Forbidden", {}, io.BytesIO(response_body))

    monkeypatch.setattr(gateway_module, "urlopen", denied_urlopen)

    with pytest.raises(RuntimeToolGatewayError) as raised:
        RuntimeToolGatewayClient._post_sync(
            "http://runtime.example/runtime/v1/tool-calls",
            {"phase": "preflight"},
            {"Authorization": "Bearer worker-token"},
        )

    assert raised.value.code == "POLICY_BLOCKED"
    assert raised.value.violation_type == "assistant_tool_not_allowed"
    assert raised.value.public_summary == "当前助手未授权使用该工具，请尝试其他方式完成请求。"


@pytest.mark.asyncio
async def test_result_retries_three_times_without_tool_output(monkeypatch):
    client = RuntimeToolGatewayClient.from_request_context(_context())
    attempts = 0

    async def fake_post(_self, _url, payload, _headers):
        nonlocal attempts
        attempts += 1
        assert "output" not in payload
        assert "result" not in payload
        if attempts < 3:
            raise RuntimeError("temporary failure")
        return {"tool_call_id": "tool_001", "status": "completed"}

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(RuntimeToolGatewayClient, "_post", fake_post)
    monkeypatch.setattr("qwenpaw.agents.runtime_tool_gateway.asyncio.sleep", no_sleep)

    result = await client.report_result("tool_001", "completed", 42)

    assert result["status"] == "completed"
    assert attempts == 3


@pytest.mark.asyncio
async def test_v1_result_failure_enters_persistent_outbox(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("QWENPAW_RUNTIME_TOOL_OUTBOX_ROOT", str(tmp_path))
    client = RuntimeToolGatewayClient.from_request_context(_v1_context())

    async def unavailable(_self, _url, _payload, _headers):
        raise RuntimeError("offline")

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(RuntimeToolGatewayClient, "_post", unavailable)
    monkeypatch.setattr(
        "qwenpaw.agents.runtime_tool_gateway.asyncio.sleep",
        no_sleep,
    )

    result = await client.report_result("tool_001", "completed", 42)

    assert result["status"] == "result_pending"
    records = list(tmp_path.glob("*.json"))
    assert len(records) == 1
    rendered = records[0].read_text(encoding="utf-8")
    assert '"status": "completed"' in rendered
    assert "output" not in rendered


@pytest.mark.asyncio
async def test_guard_callback_uses_same_call_and_only_the_decision(monkeypatch):
    client = RuntimeToolGatewayClient.from_request_context(_context())
    requests: list[dict] = []

    async def fake_post(_self, _url, payload, _headers):
        requests.append(payload)
        return {
            "tool_call_id": "tool_001",
            "guard_decision": "allow",
            "status": "executing",
        }

    monkeypatch.setattr(RuntimeToolGatewayClient, "_post", fake_post)

    result = await client.report_guard("tool_001", "allow")

    assert result["status"] == "executing"
    assert requests == [
        {
            "phase": "guard",
            "task_id": "task_001",
            "session_id": "sess_001",
            "tool_session_id": "wts_001",
            "policy_snapshot_id": "ps_001",
            "tool_call_id": "tool_001",
            "guard_decision": "allow",
        }
    ]


@pytest.mark.asyncio
async def test_guard_callback_rejects_mismatched_runtime_acknowledgement(monkeypatch):
    client = RuntimeToolGatewayClient.from_request_context(_context())

    async def fake_post(_self, _url, _payload, _headers):
        return {
            "tool_call_id": "tool_001",
            "guard_decision": "block",
            "status": "cancelled",
        }

    monkeypatch.setattr(RuntimeToolGatewayClient, "_post", fake_post)

    with pytest.raises(RuntimeToolGatewayError):
        await client.report_guard("tool_001", "allow")


def test_missing_or_other_protocol_does_not_enable_gateway():
    assert RuntimeToolGatewayClient.from_request_context({}) is None
    assert RuntimeToolGatewayClient.from_request_context({"runtime_tool_gateway": {"protocol": "legacy"}}) is None
