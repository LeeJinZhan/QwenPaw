"""Safe capability declaration for Runtime readiness probes."""

from __future__ import annotations

from fastapi import APIRouter

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
    async def get_capabilities() -> dict:
        return capability_manifest()

    return router
