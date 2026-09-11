from __future__ import annotations

import sys
import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

import bank_runtime.bank_assistant as bank_module
from bank_runtime.bank_assistant import bank_assistant
from bank_runtime.personalization import (
    BankRuntimePersonalizationCleanupHook,
    BankRuntimePersonalizationHook,
)


def _request(identity_json):
    return SimpleNamespace(
        channel="bank-runtime",
        user_id="user-a",
        identity_json=identity_json,
    )


def _ctx(identity_json):
    return SimpleNamespace(
        request=_request(identity_json),
        agent=SimpleNamespace(
            _system_prompt="BASE",
            toolkit=SimpleNamespace(tool_groups=[SimpleNamespace(tools=[])]),
        ),
        extras={},
        input_msgs=[],
        error=None,
    )


def _identity(**overrides):
    values = {
        "user_id": "user-a",
        "display_name": "测试员工",
        "org_id": "org-a",
        "roles": ["normal_user"],
        "role_codes": ["normal_user"],
        "allowed_customer_ids": [],
    }
    values.update(overrides)
    return values


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("identity", "expected"),
    [
        (None, "缺少可信银行员工身份"),
        (
            _identity(role_codes=["administrator"]),
            "员工身份角色不一致",
        ),
        (
            _identity(allowed_customer_ids=["cust-forged"]),
            "客户范围未经授权",
        ),
        (
            _identity(user_id="other-user"),
            "员工身份与请求用户不一致",
        ),
    ],
)
async def test_bank_assistant_rejects_untrusted_identity(identity, expected):
    ctx = _ctx(identity)
    await BankRuntimePersonalizationHook().run(ctx)

    result = await bank_assistant(message="查询客户")

    assert expected in result
    await BankRuntimePersonalizationCleanupHook().run(ctx)


@pytest.mark.asyncio
async def test_bank_assistant_uses_only_context_identity(monkeypatch):
    calls = []

    class FakeIdentity:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class FakeRequest:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class FakeResponse:
        reply = "受控银行回复"
        run_id = "run-001"
        session_id = "session-001"
        allowed = True
        reason_code = "ALLOW"
        invoked_tools = ["controlled_lookup"]
        result_refs = []
        artifact_refs = []
        audit_event_count = 2

    class FakeService:
        def handle(self, request):
            calls.append(request)
            return FakeResponse()

    monkeypatch.setattr(bank_module, "BankIdentity", FakeIdentity)
    monkeypatch.setattr(bank_module, "QwenPawBankRequest", FakeRequest)
    monkeypatch.setattr(bank_module, "BankAssistantService", FakeService)
    ctx = _ctx(_identity())
    await BankRuntimePersonalizationHook().run(ctx)

    result = await bank_assistant(
        message="查询授信摘要",
        session_id="session-001",
    )

    assert "受控银行回复" in result
    assert calls[0].identity.user_id == "user-a"
    assert calls[0].identity.roles == frozenset({"normal_user"})
    assert calls[0].identity.allowed_customer_ids == frozenset()
    await BankRuntimePersonalizationCleanupHook().run(ctx)


@pytest.mark.asyncio
async def test_bank_assistant_outside_bank_request_is_denied():
    assert "缺少可信银行员工身份" in await bank_assistant(message="查询客户")


def test_bank_assistant_signature_cannot_accept_model_supplied_identity():
    assert "identity_json" not in inspect.signature(bank_assistant).parameters


def test_bank_assistant_skill_keeps_authorization_out_of_prompt_guidance():
    skill = (PLUGIN_ROOT / "skills" / "bank-assistant-zh" / "SKILL.md").read_text(
        encoding="utf-8"
    )

    assert "身份只能由已认证的 Runtime" in skill
    assert "Skill 和 Prompt 只提供使用指导" in skill
    assert "不得使用 shell" in skill
