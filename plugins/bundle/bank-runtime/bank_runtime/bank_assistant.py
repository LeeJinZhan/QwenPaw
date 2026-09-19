"""Controlled bank assistant Plugin Tool using request-bound identity."""

from __future__ import annotations

from typing import Any

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
) -> str:
    """Call the controlled bank assistant for internal employee workflows.

    Args:
        message: Current employee bank-business request.
        customer_id: Optional explicit customer identifier.
        session_id: Optional current QwenPaw session for correlation.

    The model cannot supply or override employee identity. Identity is read
    only from the authenticated Bank Runtime request context.
    """
    from .personalization import current_personalization_context

    context = current_personalization_context()
    if context is None or context.identity is None:
        return (
            context.identity_error
            if context is not None and context.identity_error
            else "缺少可信银行员工身份，无法调用银行助手。"
        )
    if customer_id:
        return "客户范围未经授权，无法调用银行助手。"
    if (
        BankIdentity is None
        or BankAssistantService is None
        or QwenPawBankRequest is None
    ):
        return "银行助手服务未安装，无法调用受控银行能力。"
    identity = context.identity
    trusted_identity = BankIdentity(
        user_id=identity.user_id,
        display_name=identity.display_name,
        roles=identity.roles,
        org_id=identity.org_id,
        allowed_customer_ids=identity.allowed_customer_ids,
    )
    response = BankAssistantService().handle(
        QwenPawBankRequest(
            message=message,
            customer_id=None,
            session_id=session_id or None,
            identity=trusted_identity,
            channel="bank-runtime",
        )
    )
    return _format_response(response)


def _format_response(response: Any) -> str:
    lines = [
        str(response.reply),
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


__all__ = ["bank_assistant"]
