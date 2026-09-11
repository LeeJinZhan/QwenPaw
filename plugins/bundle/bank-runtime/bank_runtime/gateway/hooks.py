"""Lifecycle hook that applies Runtime task-scoped Driver visibility."""

from __future__ import annotations

from collections.abc import Mapping
import logging

from qwenpaw.hooks.base import LifecycleHook
from qwenpaw.runtime.hooks import HookContext, HookResult
from qwenpaw.runtime.phases import Phase

from .visibility import (
    filter_managed_driver_tools,
    parse_runtime_tool_visibility,
)


_logger = logging.getLogger("qwenpaw.plugins.bank_runtime.gateway.hooks")


class BankRuntimeToolVisibilityHook(LifecycleHook):
    phase = Phase.POST_AGENT_BUILD
    name = "bank_runtime_tool_visibility"
    priority = 35
    after = ("bank_runtime_gateway_install",)
    before = ("bank_runtime_sandbox_install",)

    async def run(self, ctx: HookContext) -> HookResult:
        if str(getattr(ctx.request, "channel", "") or "") != "bank-runtime":
            return HookResult()
        agent = ctx.agent
        if agent is None:
            raise RuntimeError("Runtime managed agent is unavailable")
        projection = parse_runtime_tool_visibility(
            getattr(ctx.request, "runtime_tool_visibility", None)
        )
        gateway = getattr(ctx.request, "runtime_tool_gateway", None)
        gateway_snapshot_hash = (
            str(gateway.get("capability_snapshot_hash") or "").strip().lower()
            if isinstance(gateway, Mapping)
            else ""
        )
        if gateway_snapshot_hash and not gateway_snapshot_hash.startswith("sha256:"):
            gateway_snapshot_hash = f"sha256:{gateway_snapshot_hash}"
        allowed_names = (
            projection.worker_tool_names
            if projection is not None
            and projection.worker_type == "qwenpaw"
            and projection.binding_snapshot_hash == gateway_snapshot_hash
            else frozenset()
        )
        projection_hash = (
            projection.binding_snapshot_hash if projection is not None else ""
        )
        _logger.debug(
            "bank-runtime visibility: projection=%s worker_type=%s "
            "projection_hash=%s gateway_hash=%s allowed=%s",
            projection is not None,
            projection.worker_type if projection is not None else "",
            projection_hash[:15],
            gateway_snapshot_hash[:15],
            sorted(allowed_names),
        )
        filter_managed_driver_tools(agent.toolkit, allowed_names)
        from .middleware import BankRuntimeGatewayMiddleware

        for middleware in getattr(agent, "_acting_middlewares", ()):
            if isinstance(middleware, BankRuntimeGatewayMiddleware):
                middleware.allowed_tool_names = allowed_names
        return HookResult()


__all__ = ["BankRuntimeToolVisibilityHook"]
