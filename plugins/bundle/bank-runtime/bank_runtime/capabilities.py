"""Safe capability declaration for Runtime readiness probes."""

from __future__ import annotations

import hmac
import os

from fastapi import APIRouter, Header, HTTPException, status

from qwenpaw.__version__ import __version__ as qwenpaw_version

from .release import BANK_RUNTIME_PLUGIN_VERSION

_CAPABILITIES = {
    "agent_scoped_chat": False,
    "managed_session": False,
    "gateway_middleware": False,
    "attachment_batch_authorize": False,
    "sandbox_file_search_select": False,
    "personal_skills": False,
    "runtime_console_readonly": False,
}
_DISABLED_FEATURES = (
    "os_shell",
    "creator",
    "browser_use",
    "computer_use",
    "external_harnesses",
    "market_plugins",
    "long_term_memory",
)


def capability_manifest() -> dict:
    """Return a fresh, data-free declaration for the skeleton release."""
    return {
        "qwenpaw_version": qwenpaw_version,
        "bank_runtime_plugin_version": BANK_RUNTIME_PLUGIN_VERSION,
        "protocols": [],
        "capabilities": dict(_CAPABILITIES),
        "disabled_features": list(_DISABLED_FEATURES),
    }


def build_capability_router() -> APIRouter:
    router = APIRouter()

    @router.get("/capabilities")
    async def get_capabilities(
        authorization: str | None = Header(default=None),
        agent_id: str | None = Header(default=None, alias="X-Agent-Id"),
    ) -> dict:
        _require_service_identity(authorization, agent_id)
        return capability_manifest()

    return router


def _require_service_identity(
    authorization: str | None,
    agent_id: str | None,
) -> None:
    configured = os.environ.get("QWENPAW_SERVICE_TOKEN", "").strip()
    if not configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Capability preflight is unavailable",
        )
    supplied = ""
    if authorization and authorization.startswith("Bearer "):
        supplied = authorization.removeprefix("Bearer ").strip()
    if (
        not supplied
        or not hmac.compare_digest(supplied, configured)
        or not str(agent_id or "").strip()
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Service identity is required",
            headers={"WWW-Authenticate": "Bearer"},
        )
