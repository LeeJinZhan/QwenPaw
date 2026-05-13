# -*- coding: utf-8 -*-
"""Tests for the bank assistant tool."""

from __future__ import annotations

import json
from importlib import import_module

import pytest

from qwenpaw.agents.tools import bank_assistant

bank_assistant_module = import_module("qwenpaw.agents.tools.bank_assistant")


@pytest.mark.asyncio
async def test_bank_assistant_tool_returns_service_reply(monkeypatch):
    calls = []

    class FakeResponse:
        reply = "客户信息：上海示例制造有限公司。"
        run_id = "run-qwenpaw-bank-001"
        session_id = "session-1"
        allowed = True
        reason_code = "ALLOW"
        invoked_tools = ["customer_get_profile"]
        result_refs = ["result_customer_get_profile_001"]
        artifact_refs = []
        audit_event_count = 2

    class FakeService:
        def handle(self, request):
            calls.append(request)
            return FakeResponse()

    monkeypatch.setattr(
        bank_assistant_module,
        "BankAssistantService",
        lambda: FakeService(),
    )

    response = await bank_assistant(
        message="查询 cust-001 的客户信息",
        customer_id="cust-001",
        session_id="session-1",
        identity_json=json.dumps(
            {
                "user_id": "u-1001",
                "display_name": "张三",
                "roles": ["postloan_manager"],
                "org_id": "branch-001",
                "allowed_customer_ids": ["cust-001"],
            },
            ensure_ascii=False,
        ),
    )

    text = response.content[0]["text"]
    assert "客户信息：上海示例制造有限公司" in text
    assert "run-qwenpaw-bank-001" in text
    assert calls[0].message == "查询 cust-001 的客户信息"
    assert calls[0].identity.user_id == "u-1001"


@pytest.mark.asyncio
async def test_bank_assistant_tool_rejects_missing_identity():
    response = await bank_assistant(
        message="查询 cust-001 的客户信息",
        customer_id="cust-001",
        session_id="session-1",
        identity_json="",
    )

    text = response.content[0]["text"]
    assert "缺少可信银行员工身份" in text


@pytest.mark.asyncio
async def test_bank_assistant_tool_rejects_invalid_identity_json():
    response = await bank_assistant(
        message="查询 cust-001 的客户信息",
        customer_id="cust-001",
        session_id="session-1",
        identity_json="{invalid",
    )

    text = response.content[0]["text"]
    assert "员工身份格式无效" in text


@pytest.mark.asyncio
async def test_bank_assistant_tool_reports_unavailable_bank_service(monkeypatch):
    monkeypatch.setattr(bank_assistant_module, "BankIdentity", None)

    response = await bank_assistant(
        message="查询 cust-001 的客户信息",
        customer_id="cust-001",
        session_id="session-1",
        identity_json=json.dumps(
            {
                "user_id": "u-1001",
                "display_name": "张三",
                "roles": ["postloan_manager"],
                "org_id": "branch-001",
                "allowed_customer_ids": ["cust-001"],
            },
            ensure_ascii=False,
        ),
    )

    text = response.content[0]["text"]
    assert "银行助手服务未安装" in text
