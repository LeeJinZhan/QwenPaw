"""QwenPaw lifecycle boundary for the managed MinerU MCP listener."""

from __future__ import annotations

import importlib

from qwenpaw.plugins.api import PluginApi

from .bank_mineru_mcp.config import MinerUSettings
from .bank_mineru_mcp.server import build_mineru_mcp_service


class BankMinerUMcpPlugin:
    def __init__(self) -> None:
        self._registered = False
        self._service = None

    def register(self, api: PluginApi) -> None:
        if self._registered:
            raise RuntimeError("Bank MinerU MCP plugin is already registered")
        api.register_startup_hook(
            hook_name="bank_mineru_mcp_start",
            callback=self._start,
            priority=900,
        )
        api.register_shutdown_hook(
            hook_name="bank_mineru_mcp_stop",
            callback=self._stop,
            priority=100,
        )
        api.register_uninstall_hook(
            hook_name="bank_mineru_mcp_uninstall",
            callback=self._stop_and_cleanup,
            priority=100,
        )
        self._registered = True

    async def _start(self) -> None:
        if self._service is not None:
            return
        resolver_module = importlib.import_module("bank_runtime.sandbox.file_refs")
        resolver_factory = getattr(resolver_module, "get_file_ref_registry", None)
        if not callable(resolver_factory):
            raise RuntimeError("bank-runtime file reference resolver is unavailable")
        settings = MinerUSettings.from_environment()
        service = build_mineru_mcp_service(
            settings,
            file_resolver=resolver_factory(),
        )
        await service.start()
        self._service = service

    async def _stop(self) -> None:
        service = self._service
        self._service = None
        if service is not None:
            await service.stop()

    async def _stop_and_cleanup(self, **_: object) -> None:
        service = self._service
        await self._stop()
        if service is not None:
            service.cleanup_results()


plugin = BankMinerUMcpPlugin()

__all__ = ["BankMinerUMcpPlugin", "plugin"]
