# -*- coding: utf-8 -*-
"""User-scoped, read-only Runtime conversation proxy for the console."""

from __future__ import annotations

import ipaddress
import os
import re
import secrets
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
DEFAULT_RUNTIME_APP_ID = "bank_user_console"
DEFAULT_LOCAL_RUNTIME_APP_TOKEN = "local-user-console-token"
DEFAULT_RUNTIME_APP_SCOPES = "assistant:read"
EXTERNAL_USER_ID_HEADER = "X-Runtime-External-User-Id"
EXTERNAL_ORG_ID_HEADER = "X-Runtime-External-Org-Id"
EXTERNAL_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}$"


class RuntimeConnectRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=128, pattern=EXTERNAL_ID_PATTERN)
    org_id: str = Field(min_length=1, max_length=128, pattern=EXTERNAL_ID_PATTERN)


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


def _runtime_app_headers() -> dict[str, str]:
    base_url = _runtime_base_url()
    app_id = os.environ.get("QWENPAW_RUNTIME_APP_ID", DEFAULT_RUNTIME_APP_ID).strip()
    scopes = os.environ.get(
        "QWENPAW_RUNTIME_APP_SCOPES",
        DEFAULT_RUNTIME_APP_SCOPES,
    ).strip()
    app_token = os.environ.get("QWENPAW_RUNTIME_APP_TOKEN", "").strip()
    if not app_token and _is_loopback_host(urlparse(base_url).hostname or ""):
        app_token = DEFAULT_LOCAL_RUNTIME_APP_TOKEN
    if not app_id or not scopes or not app_token:
        raise HTTPException(status_code=503, detail="Runtime is unavailable")
    return {
        "Authorization": f"Bearer {app_token}",
        "X-App-Id": app_id,
        "X-App-Scopes": scopes,
    }


def _external_identity(user_id: str | None, org_id: str | None) -> dict[str, str]:
    normalized_user_id = str(user_id or "").strip()
    normalized_org_id = str(org_id or "").strip()
    if not normalized_user_id or not normalized_org_id:
        raise HTTPException(status_code=401, detail="Runtime identity required")
    if not re.fullmatch(EXTERNAL_ID_PATTERN, normalized_user_id) or not re.fullmatch(
        EXTERNAL_ID_PATTERN,
        normalized_org_id,
    ):
        raise HTTPException(status_code=400, detail="Runtime identity is invalid")
    return {"user_id": normalized_user_id, "org_id": normalized_org_id}


async def _runtime_request(
    method: str,
    path: str,
    *,
    external_identity: dict[str, str],
    json_body: dict[str, Any] | None = None,
) -> Any:
    headers = {
        "Accept": "application/json",
        "X-Request-Id": f"req_qwenpaw_{secrets.token_urlsafe(12)}",
        "X-External-User-Id": external_identity["user_id"],
        "X-External-Org-Id": external_identity["org_id"],
        **_runtime_app_headers(),
    }
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


@router.post("/connect")
async def connect_runtime_console(body: RuntimeConnectRequest) -> dict[str, Any]:
    identity = _external_identity(body.user_id, body.org_id)
    await _runtime_request(
        "GET",
        "/api/v1/conversations?page=1&page_size=1",
        external_identity=identity,
    )
    return {"connected": True, "identity": identity}


@router.get("/conversations")
async def list_runtime_conversations(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=100, ge=1, le=100),
    external_user_id: str | None = Header(
        default=None,
        alias=EXTERNAL_USER_ID_HEADER,
    ),
    external_org_id: str | None = Header(
        default=None,
        alias=EXTERNAL_ORG_ID_HEADER,
    ),
) -> Any:
    identity = _external_identity(external_user_id, external_org_id)
    return await _runtime_request(
        "GET",
        f"/api/v1/conversations?page={page}&page_size={page_size}",
        external_identity=identity,
    )


@router.get("/conversations/{conversation_id}")
async def get_runtime_conversation(
    conversation_id: str,
    external_user_id: str | None = Header(
        default=None,
        alias=EXTERNAL_USER_ID_HEADER,
    ),
    external_org_id: str | None = Header(
        default=None,
        alias=EXTERNAL_ORG_ID_HEADER,
    ),
) -> Any:
    identity = _external_identity(external_user_id, external_org_id)
    encoded_id = quote(conversation_id, safe="")
    return await _runtime_request(
        "GET",
        f"/api/v1/conversations/{encoded_id}",
        external_identity=identity,
    )
