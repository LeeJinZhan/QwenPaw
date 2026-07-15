# -*- coding: utf-8 -*-
"""Tests for the Runtime Tool Gateway callback client."""

from __future__ import annotations

import pytest

from qwenpaw.agents.runtime_tool_gateway import RuntimeToolGatewayClient


def _context() -> dict:
    return {
        "runtime_tool_gateway": {
            "protocol": "preflight_result_v1",
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


@pytest.mark.asyncio
async def test_preflight_uses_task_gateway_context_and_returns_allow(monkeypatch):
    client = RuntimeToolGatewayClient.from_request_context(_context())
    requests: list[tuple[str, dict, dict]] = []

    async def fake_post(_self, url, payload, headers):
        requests.append((url, payload, headers))
        return {"tool_call_id": "tool_001", "decision": "allow", "status": "allowed", "trace_id": "trace_001"}

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
                "tool_id": "mcp.policy.search",
                "input": {"query": "制度"},
                "idempotency_key": result["idempotency_key"],
                "trace_id": "trace_001",
            },
            {"Authorization": "Bearer worker-token"},
        )
    ]


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


def test_missing_or_other_protocol_does_not_enable_gateway():
    assert RuntimeToolGatewayClient.from_request_context({}) is None
    assert RuntimeToolGatewayClient.from_request_context({"runtime_tool_gateway": {"protocol": "legacy"}}) is None
