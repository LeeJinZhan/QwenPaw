"""Backend entry point for the QwenPaw Ops Observer Plugin."""

from __future__ import annotations

from .observer import OpsObserverMiddleware
from .router import build_router
from .storage import ObserverService


class OpsObserverPlugin:
    """Register public QwenPaw Plugin API extensions only."""

    def __init__(self) -> None:
        self._service: ObserverService | None = None

    def register(self, api) -> None:
        self._service = ObserverService.from_environment(api.config)
        api.register_startup_hook("ops_observer_start", self._service.start)
        api.register_shutdown_hook("ops_observer_stop", self._service.stop)
        api.register_uninstall_hook("ops_observer_uninstall", self._service.stop)
        api.register_middleware(self._middleware_factory, priority=80)
        api.register_http_router(
            build_router(self._service),
            prefix="/ops-observer",
            tags=["ops-observer"],
        )

    def _middleware_factory(self, ctx, _agent_config):
        if self._service is None:
            return None
        return OpsObserverMiddleware(self._service, ctx)


plugin = OpsObserverPlugin()
