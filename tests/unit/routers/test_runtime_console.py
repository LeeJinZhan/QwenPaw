# -*- coding: utf-8 -*-
"""Runtime history proxy keeps external identity at the Runtime boundary."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from qwenpaw.app.routers import runtime_console


@pytest.mark.asyncio
async def test_runtime_console_connect_probes_external_conversations(
    monkeypatch,
) -> None:
    forwarded = AsyncMock(return_value={"items": [], "total": 0})
    monkeypatch.setattr(runtime_console, "_runtime_request", forwarded)

    response = await runtime_console.connect_runtime_console(
        runtime_console.RuntimeConnectRequest(
            user_id="u001",
            org_id="org001",
        ),
    )

    assert response == {
        "connected": True,
        "identity": {"user_id": "u001", "org_id": "org001"},
    }
    forwarded.assert_awaited_once_with(
        "GET",
        "/bank-agent-runtime/api/v1/conversations?page=1&page_size=1",
        external_identity={"user_id": "u001", "org_id": "org001"},
    )


@pytest.mark.asyncio
async def test_runtime_console_uses_configured_runtime_context_path(monkeypatch) -> None:
    forwarded = AsyncMock(return_value={"items": [], "total": 0})
    monkeypatch.setattr(runtime_console, "_runtime_request", forwarded)
    monkeypatch.setenv("QWENPAW_RUNTIME_CONTEXT_PATH", "/bank-runtime-lab")

    await runtime_console.connect_runtime_console(
        runtime_console.RuntimeConnectRequest(user_id="u001", org_id="org001")
    )

    forwarded.assert_awaited_once_with(
        "GET",
        "/bank-runtime-lab/api/v1/conversations?page=1&page_size=1",
        external_identity={"user_id": "u001", "org_id": "org001"},
    )


def test_runtime_console_rejects_invalid_runtime_context_path(monkeypatch) -> None:
    monkeypatch.setenv("QWENPAW_RUNTIME_CONTEXT_PATH", "/bank-runtime/")

    with pytest.raises(HTTPException) as exc_info:
        runtime_console._runtime_api_path("/conversations")

    assert exc_info.value.status_code == 503

@pytest.mark.asyncio
async def test_runtime_console_conversations_require_external_identity() -> None:
    with pytest.raises(HTTPException) as exc_info:
        await runtime_console.list_runtime_conversations(
            page=1,
            page_size=20,
            external_user_id=None,
            external_org_id=None,
        )

    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_runtime_console_conversations_reject_invalid_external_identity() -> None:
    with pytest.raises(HTTPException) as exc_info:
        await runtime_console.list_runtime_conversations(
            page=1,
            page_size=20,
            external_user_id="u001 with space",
            external_org_id="org001",
        )

    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_runtime_console_conversations_forward_external_identity(
    monkeypatch,
) -> None:
    forwarded = AsyncMock(return_value={"items": [], "total": 0})
    monkeypatch.setattr(runtime_console, "_runtime_request", forwarded)

    response = await runtime_console.list_runtime_conversations(
        page=2,
        page_size=50,
        external_user_id="u001",
        external_org_id="org001",
    )

    assert response == {"items": [], "total": 0}
    forwarded.assert_awaited_once_with(
        "GET",
        "/bank-agent-runtime/api/v1/conversations?page=2&page_size=50",
        external_identity={"user_id": "u001", "org_id": "org001"},
    )


@pytest.mark.asyncio
async def test_runtime_console_detail_encodes_conversation_id(
    monkeypatch,
) -> None:
    forwarded = AsyncMock(return_value={"conversation_id": "conv/001"})
    monkeypatch.setattr(runtime_console, "_runtime_request", forwarded)

    response = await runtime_console.get_runtime_conversation(
        conversation_id="conv/001",
        external_user_id="u001",
        external_org_id="org001",
    )

    assert response == {"conversation_id": "conv/001"}
    forwarded.assert_awaited_once_with(
        "GET",
        "/bank-agent-runtime/api/v1/conversations/conv%2F001",
        external_identity={"user_id": "u001", "org_id": "org001"},
    )
