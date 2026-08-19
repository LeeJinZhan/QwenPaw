"""Local/dev read-only Runtime conversation PawApp."""

from __future__ import annotations

import ipaddress
import os
import re
import secrets
from typing import Any
from urllib.parse import quote, urlparse

import httpx
from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from qwenpaw.pawapp import PawApp

_EXTERNAL_ID = r"^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}$"
_USER_HEADER = "X-Runtime-External-User-Id"
_ORG_HEADER = "X-Runtime-External-Org-Id"


class RuntimeConnectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(min_length=1, max_length=128, pattern=_EXTERNAL_ID)
    org_id: str = Field(min_length=1, max_length=128, pattern=_EXTERNAL_ID)


router = APIRouter()


def _is_loopback(hostname: str) -> bool:
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _runtime_config() -> tuple[str, str, str, str]:
    base_url = (
        str(os.environ.get("QWENPAW_RUNTIME_CONSOLE_BASE_URL") or "")
        .strip()
        .rstrip("/")
    )
    app_id = str(os.environ.get("QWENPAW_RUNTIME_CONSOLE_APP_ID") or "").strip()
    app_token = str(os.environ.get("QWENPAW_RUNTIME_CONSOLE_APP_TOKEN") or "").strip()
    scopes = str(
        os.environ.get("QWENPAW_RUNTIME_CONSOLE_APP_SCOPES") or "assistant:read"
    ).strip()
    parsed = urlparse(base_url)
    allow_internal_http = str(
        os.environ.get("QWENPAW_RUNTIME_CONSOLE_ALLOW_INTERNAL_HTTP") or ""
    ).lower() in {"1", "true", "yes", "on"}
    if (
        not base_url
        or parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or not app_id
        or not app_token
        or not scopes
        or (
            parsed.scheme == "http"
            and not _is_loopback(parsed.hostname)
            and not allow_internal_http
        )
    ):
        raise HTTPException(status_code=503, detail="Runtime console is unavailable")
    return base_url, app_id, app_token, scopes


def _runtime_headers() -> dict[str, str]:
    _base_url, app_id, app_token, scopes = _runtime_config()
    return {
        "Authorization": f"Bearer {app_token}",
        "X-App-Id": app_id,
        "X-App-Scopes": scopes,
    }


def _identity(user_id: str | None, org_id: str | None) -> dict[str, str]:
    user = str(user_id or "").strip()
    org = str(org_id or "").strip()
    if not user or not org:
        raise HTTPException(status_code=401, detail="Runtime identity required")
    if not re.fullmatch(_EXTERNAL_ID, user) or not re.fullmatch(_EXTERNAL_ID, org):
        raise HTTPException(status_code=400, detail="Runtime identity is invalid")
    return {"user_id": user, "org_id": org}


async def _runtime_request(
    method: str,
    path: str,
    *,
    external_identity: dict[str, str],
) -> Any:
    base_url, _app_id, _app_token, _scopes = _runtime_config()
    headers = {
        **_runtime_headers(),
        "Accept": "application/json",
        "X-Request-Id": f"req_qwenpaw_{secrets.token_urlsafe(12)}",
        "X-External-User-Id": external_identity["user_id"],
        "X-External-Org-Id": external_identity["org_id"],
    }
    try:
        async with httpx.AsyncClient(
            base_url=base_url,
            timeout=15,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = await client.request(method, path, headers=headers)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Runtime is unavailable") from exc
    try:
        payload = response.json()
    except ValueError as exc:
        raise HTTPException(
            status_code=502,
            detail="Runtime returned an invalid response",
        ) from exc
    if response.is_error:
        raise HTTPException(
            status_code=response.status_code,
            detail="Runtime request failed",
        )
    return payload


@router.post("/connect")
async def connect(body: dict[str, Any]) -> dict[str, Any]:
    try:
        validated = RuntimeConnectRequest.model_validate(body)
    except ValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail="Runtime connect request is invalid",
        ) from exc
    identity = _identity(validated.user_id, validated.org_id)
    await _runtime_request(
        "GET",
        "/api/v1/conversations?page=1&page_size=1",
        external_identity=identity,
    )
    return {"connected": True, "identity": identity}


@router.get("/conversations")
async def conversations(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=100, ge=1, le=100),
    external_user_id: str | None = Header(default=None, alias=_USER_HEADER),
    external_org_id: str | None = Header(default=None, alias=_ORG_HEADER),
) -> Any:
    identity = _identity(external_user_id, external_org_id)
    return await _runtime_request(
        "GET",
        f"/api/v1/conversations?page={page}&page_size={page_size}",
        external_identity=identity,
    )


@router.get("/conversations/{conversation_id}")
async def conversation(
    conversation_id: str,
    external_user_id: str | None = Header(default=None, alias=_USER_HEADER),
    external_org_id: str | None = Header(default=None, alias=_ORG_HEADER),
) -> Any:
    identity = _identity(external_user_id, external_org_id)
    return await _runtime_request(
        "GET",
        f"/api/v1/conversations/{quote(conversation_id, safe='')}",
        external_identity=identity,
    )


app = PawApp(name="Bank Runtime Console", app_id="bank-runtime-console")
app.include_router(router)
plugin = app

__all__ = ["app", "plugin", "router"]
