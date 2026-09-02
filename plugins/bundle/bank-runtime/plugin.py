"""Bank Agent Runtime plugin registration boundary."""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi import APIRouter

from qwenpaw.plugins.api import PluginApi

# ``qwenpaw plugin install`` executes this entry point as a dynamically
# loaded module.  The plugin's sibling ``bank_runtime`` package therefore is
# not importable unless the installation directory is explicitly exposed.
# QwenPaw's loader removes this path again on unload or failed registration.
_PLUGIN_DIR = str(Path(__file__).resolve().parent)
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

from bank_runtime.capabilities import build_capability_router
from bank_runtime.channel import BankRuntimeChannel
from bank_runtime.hooks import bank_runtime_startup_guard
from bank_runtime.middleware import bank_runtime_middleware_factory
from bank_runtime.gateway.middleware import BankRuntimeGatewayInstallHook
from bank_runtime.gateway.hooks import BankRuntimeToolVisibilityHook
from bank_runtime.sandbox.hooks import (
    BankRuntimeAttachmentPrepareHook,
    BankRuntimeSandboxCleanupHook,
    BankRuntimeSandboxInstallHook,
)
from bank_runtime.bank_assistant import bank_assistant
from bank_runtime.artifact_tools import (
    ArtifactDeliveryErrorHook,
    artifact_convert,
    artifact_generate,
    artifact_revise,
    template_fill_docx,
)
from bank_runtime.personal_skills import activate_personal_skill
from bank_runtime.personalization import (
    BankRuntimePersonalizationCleanupHook,
    BankRuntimePersonalizationHook,
    BankRuntimePersonalizationRedactionHook,
)
from bank_runtime.router import build_ingress_router
from bank_runtime.session import (
    ManagedSessionCleanupHook,
    ManagedSessionCommitHook,
    ManagedSessionDisableLongTermMemoryHook,
    ManagedSessionErrorHook,
    ManagedSessionPrepareHook,
)


def _build_http_router() -> APIRouter:
    router = APIRouter()
    router.include_router(build_capability_router())
    router.include_router(build_ingress_router())
    return router


class BankRuntimePlugin:
    """Register the fail-closed Bank Runtime candidate integration."""

    def __init__(self) -> None:
        self._registered = False

    def register(self, api: PluginApi) -> None:
        if self._registered:
            raise RuntimeError("Bank Runtime plugin is already registered")

        # Register resources with native uniqueness enforcement first. If a
        # hot-reload creates another plugin instance, the HTTP prefix or
        # channel collision aborts before hooks and middleware are appended.
        api.register_http_router(
            _build_http_router(),
            prefix="/bank-runtime",
            tags=["bank-runtime"],
        )
        api.register_channel(
            channel_class=BankRuntimeChannel,
            label="Bank Runtime",
            description="Runtime-managed requests only",
        )
        api.register_runtime_hook(ManagedSessionPrepareHook())
        api.register_runtime_hook(ManagedSessionDisableLongTermMemoryHook())
        api.register_runtime_hook(BankRuntimePersonalizationHook())
        api.register_runtime_hook(BankRuntimeGatewayInstallHook())
        api.register_runtime_hook(BankRuntimeToolVisibilityHook())
        api.register_runtime_hook(BankRuntimeSandboxInstallHook())
        api.register_runtime_hook(BankRuntimeAttachmentPrepareHook())
        api.register_runtime_hook(BankRuntimePersonalizationRedactionHook())
        api.register_runtime_hook(ManagedSessionCommitHook())
        api.register_runtime_hook(ManagedSessionErrorHook())
        api.register_runtime_hook(ArtifactDeliveryErrorHook())
        api.register_runtime_hook(BankRuntimePersonalizationCleanupHook())
        api.register_runtime_hook(BankRuntimeSandboxCleanupHook())
        api.register_runtime_hook(ManagedSessionCleanupHook())
        api.register_startup_hook(
            hook_name="bank_runtime_startup_guard",
            callback=bank_runtime_startup_guard,
            # Plugin tools and workspace skill providers use priorities 50+
            # and 100. Audit the final registry rather than a partial view.
            priority=1000,
        )
        api.register_middleware(
            bank_runtime_middleware_factory,
            priority=10,
        )
        api.register_tool(
            tool_name="bank_assistant",
            tool_func=bank_assistant,
            description="Controlled internal bank assistant",
            icon="🏦",
            enabled=False,
            tool_type="internal",
        )
        api.register_tool(
            tool_name="activate_personal_skill",
            tool_func=activate_personal_skill,
            description="Load one request-scoped Personal Skill",
            icon="🧩",
            enabled=False,
            tool_type="network",
            target_param="skill_ref",
        )
        api.register_tool(
            tool_name="artifact_generate",
            tool_func=artifact_generate,
            description="Generate a Runtime-governed office artifact",
            icon="📄",
            enabled=False,
            tool_type="internal",
        )
        api.register_tool(
            tool_name="artifact_revise",
            tool_func=artifact_revise,
            description="Create a new version of a Runtime-generated artifact",
            icon="📝",
            enabled=False,
            tool_type="internal",
        )
        api.register_tool(
            tool_name="artifact_convert",
            tool_func=artifact_convert,
            description="Convert a Runtime-generated artifact",
            icon="🔄",
            enabled=False,
            tool_type="internal",
        )
        api.register_tool(
            tool_name="template_fill_docx",
            tool_func=template_fill_docx,
            description="Fill an authorized published DOCX template",
            icon="📑",
            enabled=False,
            tool_type="internal",
        )
        api.register_skill_provider(
            skills_dir=Path(__file__).parent / "skills",
            enabled_by_default=True,
            channels=["bank-runtime"],
        )
        self._registered = True


plugin = BankRuntimePlugin()
