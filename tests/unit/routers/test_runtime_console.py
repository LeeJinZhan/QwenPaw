# -*- coding: utf-8 -*-
"""Runtime history proxy keeps user authorization at the Runtime boundary."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from qwenpaw.app.routers import runtime_console


@pytest.mark.asyncio
async def test_runtime_console_login_forwards_only_to_configured_runtime(
    monkeypatch,
) -> None:
    forwarded = AsyncMock(return_value={"access_token": "runtime-user-token"})
    monkeypatch.setattr(runtime_console, "_runtime_request", forwarded)

    response = await runtime_console.login_runtime_console(
        runtime_console.RuntimeLoginRequest(
            username="u001",
            password="Password123!",
        ),
    )

    assert response == {"access_token": "runtime-user-token"}
    forwarded.assert_awaited_once_with(
        "POST",
        "/runtime/auth/login",
        json_body={"username": "u001", "password": "Password123!"},
    )


@pytest.mark.asyncio
async def test_runtime_console_conversations_require_user_token() -> None:
    with pytest.raises(HTTPException) as exc_info:
        await runtime_console.list_runtime_conversations(
            page=1,
            page_size=20,
            runtime_user_token=None,
        )

    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_runtime_console_conversations_forward_current_user_token(
    monkeypatch,
) -> None:
    forwarded = AsyncMock(return_value={"items": [], "total": 0})
    monkeypatch.setattr(runtime_console, "_runtime_request", forwarded)

    response = await runtime_console.list_runtime_conversations(
        page=2,
        page_size=50,
        runtime_user_token="runtime-user-token",
    )

    assert response == {"items": [], "total": 0}
    forwarded.assert_awaited_once_with(
        "GET",
        "/runtime/user/conversations?page=2&page_size=50",
        runtime_user_token="runtime-user-token",
    )


@pytest.mark.asyncio
async def test_runtime_console_detail_encodes_conversation_id(
    monkeypatch,
) -> None:
    forwarded = AsyncMock(return_value={"conversation_id": "conv/001"})
    monkeypatch.setattr(runtime_console, "_runtime_request", forwarded)

    response = await runtime_console.get_runtime_conversation(
        conversation_id="conv/001",
        runtime_user_token="runtime-user-token",
    )

    assert response == {"conversation_id": "conv/001"}
    forwarded.assert_awaited_once_with(
        "GET",
        "/runtime/user/conversations/conv%2F001",
        runtime_user_token="runtime-user-token",
    )
