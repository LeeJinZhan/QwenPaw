"""Safe capability declaration for Runtime readiness probes."""

from __future__ import annotations

from fastapi import APIRouter, Header

from qwenpaw.__version__ import __version__ as qwenpaw_version

from .release import BANK_RUNTIME_PLUGIN_VERSION
from .auth import require_service_identity

_CAPABILITIES = {
    "agent_scoped_chat": True,
    "managed_session": True,
    "gateway_middleware": True,
    "attachment_batch_authorize": True,
    "sandbox_file_search_select": True,
    "personal_skills": True,
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
        "protocols": [
            "bank-runtime-text-sse/v1",
            "session/2.0",
            "profile/1.0",
            "personal-skills/1.0",
            "bank-assistant/1.0",
            "tool-gateway/2.0",
            "sandbox-files/2.0",
            "physical-sandbox/1.0",
        ],
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
        require_service_identity(
            authorization,
            agent_id,
            unavailable_detail="Capability preflight is unavailable",
        )
        return capability_manifest()

    return router
