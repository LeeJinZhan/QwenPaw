# -*- coding: utf-8 -*-
"""User-scoped, read-only Runtime conversation proxy for the console."""

from __future__ import annotations

import ipaddress
import os
from typing import Any
from urllib.parse import quote, urlparse

import httpx
from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, Field


router = APIRouter(prefix="/runtime-console", tags=["runtime-console"])

RUNTIME_BASE_URL_ENV_KEYS = (
    "QWENPAW_RUNTIME_BASE_URL",
    "BANK_RUNTIME_BASE_URL",
    "RUNTIME_BASE_URL",
)
DEFAULT_LOCAL_RUNTIME_BASE_URL = "http://127.0.0.1:8765"
RUNTIME_USER_TOKEN_HEADER = "X-Runtime-User-Token"


class RuntimeLoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=512)


def _is_loopback_host(hostname: str) -> bool:
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _runtime_base_url() -> str:
    configured = next(
        (
            os.environ[key].strip()
            for key in RUNTIME_BASE_URL_ENV_KEYS
            if os.environ.get(key, "").strip()
        ),
        DEFAULT_LOCAL_RUNTIME_BASE_URL,
    ).rstrip("/")
    parsed = urlparse(configured)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise HTTPException(status_code=503, detail="Runtime is unavailable")
    allow_internal_http = os.environ.get(
        "QWENPAW_RUNTIME_ALLOW_INTERNAL_HTTP",
        "",
    ).strip().lower() in {"1", "true", "yes", "on"}
    if (
        parsed.scheme == "http"
        and not _is_loopback_host(parsed.hostname)
        and not allow_internal_http
    ):
        raise HTTPException(status_code=503, detail="Runtime is unavailable")
    return configured


def _require_runtime_user_token(token: str | None) -> str:
    normalized = str(token or "").strip()
    if not normalized or len(normalized) > 4096:
        raise HTTPException(status_code=401, detail="Runtime login required")
    return normalized


async def _runtime_request(
    method: str,
    path: str,
    *,
    runtime_user_token: str = "",
    json_body: dict[str, Any] | None = None,
) -> Any:
    headers = {"Accept": "application/json"}
    if runtime_user_token:
        headers["Authorization"] = f"Bearer {runtime_user_token}"
    try:
        async with httpx.AsyncClient(
            base_url=_runtime_base_url(),
            timeout=httpx.Timeout(15.0),
            follow_redirects=False,
        ) as client:
            response = await client.request(
                method,
                path,
                headers=headers,
                json=json_body,
            )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail="Runtime is unavailable",
        ) from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise HTTPException(
            status_code=502,
            detail="Runtime returned an invalid response",
        ) from exc
    if response.is_error:
        public_message = "Runtime request failed"
        if isinstance(payload, dict):
            detail = payload.get("detail")
            if isinstance(detail, dict):
                public_message = str(detail.get("message") or public_message)
            elif isinstance(detail, str) and detail.strip():
                public_message = detail.strip()
        raise HTTPException(
            status_code=response.status_code,
            detail=public_message,
        )
    return payload


@router.post("/login")
async def login_runtime_console(body: RuntimeLoginRequest) -> Any:
    return await _runtime_request(
        "POST",
        "/runtime/auth/login",
        json_body={"username": body.username, "password": body.password},
    )


@router.get("/conversations")
async def list_runtime_conversations(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=100, ge=1, le=100),
    runtime_user_token: str | None = Header(
        default=None,
        alias=RUNTIME_USER_TOKEN_HEADER,
    ),
) -> Any:
    token = _require_runtime_user_token(runtime_user_token)
    return await _runtime_request(
        "GET",
        f"/runtime/user/conversations?page={page}&page_size={page_size}",
        runtime_user_token=token,
    )


@router.get("/conversations/{conversation_id}")
async def get_runtime_conversation(
    conversation_id: str,
    runtime_user_token: str | None = Header(
        default=None,
        alias=RUNTIME_USER_TOKEN_HEADER,
    ),
) -> Any:
    token = _require_runtime_user_token(runtime_user_token)
    encoded_id = quote(conversation_id, safe="")
    return await _runtime_request(
        "GET",
        f"/runtime/user/conversations/{encoded_id}",
        runtime_user_token=token,
    )
