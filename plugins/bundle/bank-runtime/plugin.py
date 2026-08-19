"""Bank Agent Runtime plugin registration boundary."""

from __future__ import annotations

from fastapi import APIRouter

from qwenpaw.plugins.api import PluginApi

from bank_runtime.capabilities import build_capability_router
from bank_runtime.channel import BankRuntimeChannel
from bank_runtime.hooks import bank_runtime_startup_guard
from bank_runtime.middleware import bank_runtime_middleware_factory
from bank_runtime.router import build_ingress_router


def _build_http_router() -> APIRouter:
    router = APIRouter()
    router.include_router(build_capability_router())
    router.include_router(build_ingress_router())
    return router


class BankRuntimePlugin:
    """Register only the fail-closed Task 2 integration skeleton."""

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
        api.register_startup_hook(
            hook_name="bank_runtime_startup_guard",
            callback=bank_runtime_startup_guard,
            priority=10,
        )
        api.register_middleware(
            bank_runtime_middleware_factory,
            priority=10,
        )
        self._registered = True


plugin = BankRuntimePlugin()
