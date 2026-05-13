# -*- coding: utf-8 -*-
"""QwenPaw tool for the controlled bank assistant."""

from __future__ import annotations

import json
from typing import Any

from agentscope.message import TextBlock
from agentscope.tool import ToolResponse

try:
    from bank_agent.bank_control.identity import BankIdentity
    from bank_agent.qwenpaw_integration.service import (
        BankAssistantService,
        QwenPawBankRequest,
    )
except ImportError:
    BankIdentity = None
    BankAssistantService = None
    QwenPawBankRequest = None


async def bank_assistant(
    message: str,
    customer_id: str | None = None,
    session_id: str | None = None,
    identity_json: str = "",
) -> ToolResponse:
    """Call the controlled bank assistant for internal employee workflows.

    Args:
        message: The employee's bank-business request in natural language.
        customer_id: Optional explicit customer id, such as `cust-001`.
        session_id: Optional QwenPaw chat/session id for run correlation.
        identity_json: Trusted employee identity JSON supplied by the channel
            or runtime. It must include user_id, display_name, roles, org_id,
            and allowed_customer_ids.

    Returns:
        `ToolResponse`: A business reply plus controlled run metadata.
    """
    identity, identity_error = _parse_identity(identity_json)
    if identity_error is not None:
        return _text_response(identity_error)

    if BankAssistantService is None or QwenPawBankRequest is None:
        return _text_response("银行助手服务未安装，无法调用受控银行能力。")

    response = BankAssistantService().handle(
        QwenPawBankRequest(
            message=message,
            customer_id=customer_id or None,
            session_id=session_id or None,
            identity=identity,
            channel="qwenpaw",
        )
    )

    return _text_response(_format_response_text(response))


def _parse_identity(identity_json: str) -> tuple[BankIdentity | None, str | None]:
    if not identity_json.strip():
        return None, "缺少可信银行员工身份，无法调用银行助手。"

    if BankIdentity is None:
        return None, "银行助手服务未安装，无法解析可信银行员工身份。"

    try:
        payload = json.loads(identity_json)
    except json.JSONDecodeError:
        return None, "员工身份格式无效，无法调用银行助手。"

    if not isinstance(payload, dict):
        return None, "员工身份格式无效，无法调用银行助手。"

    try:
        identity = BankIdentity(
            user_id=str(payload["user_id"]),
            display_name=str(payload["display_name"]),
            roles=frozenset(_as_str_list(payload["roles"])),
            org_id=str(payload["org_id"]),
            allowed_customer_ids=frozenset(
                _as_str_list(payload["allowed_customer_ids"])
            ),
        )
    except (KeyError, TypeError, ValueError):
        return None, "员工身份字段不完整，无法调用银行助手。"

    return identity, None


def _as_str_list(value: Any) -> list[str]:
    if not isinstance(value, list | tuple | set):
        raise TypeError("identity field must be a list")
    return [str(item) for item in value]


def _format_response_text(response: Any) -> str:
    lines = [
        response.reply,
        "",
        f"run_id: {response.run_id}",
        f"session_id: {response.session_id}",
        f"allowed: {response.allowed}",
        f"reason_code: {response.reason_code}",
        "invoked_tools: " + ", ".join(response.invoked_tools),
    ]
    if response.result_refs:
        lines.append("result_refs: " + ", ".join(response.result_refs))
    if response.artifact_refs:
        lines.append("artifact_refs: " + ", ".join(response.artifact_refs))
    lines.append(f"audit_event_count: {response.audit_event_count}")
    return "\n".join(lines)


def _text_response(text: str) -> ToolResponse:
    return ToolResponse(content=[TextBlock(type="text", text=text)])
