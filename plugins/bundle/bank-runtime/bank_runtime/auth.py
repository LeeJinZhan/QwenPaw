"""Trusted Runtime service identity checks shared by plugin endpoints."""

from __future__ import annotations

import hmac
import os

from fastapi import HTTPException, status


def require_service_identity(
    authorization: str | None,
    agent_id: str | None,
    *,
    unavailable_detail: str = "Bank Runtime ingress is unavailable",
) -> str:
    """Return the trusted agent id or fail closed without leaking secrets."""
    configured = os.environ.get("QWENPAW_SERVICE_TOKEN", "").strip()
    if not configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=unavailable_detail,
        )
    supplied = ""
    if authorization and authorization.startswith("Bearer "):
        supplied = authorization.removeprefix("Bearer ").strip()
    normalized_agent_id = str(agent_id or "").strip()
    if (
        not supplied
        or not hmac.compare_digest(supplied, configured)
        or not normalized_agent_id
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Service identity is required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return normalized_agent_id
