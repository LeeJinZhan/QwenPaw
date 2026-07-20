"""Controlled HTTP event ingress and stats API for Ops Observer."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict

from .storage import ObserverService


class UserBehaviorEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_type: str
    occurred_at: datetime
    event_key: str = ""


def build_router(service: ObserverService) -> APIRouter:
    router = APIRouter()

    @router.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @router.post("/events", status_code=202)
    async def post_event(event: UserBehaviorEvent, x_ops_observer_token: str | None = Header(default=None)) -> dict[str, str]:
        if not service.accepts_event_token(x_ops_observer_token):
            raise HTTPException(status_code=401, detail="unauthorized")
        timestamp = event.occurred_at.astimezone(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
        if not service.enqueue_user_event(event.event_type, timestamp, event.event_key):
            raise HTTPException(status_code=400, detail="invalid_event")
        return {"status": "accepted"}

    # ── Stats endpoints (read-only, consumed by the console dashboard) ──

    @router.get("/stats/overview")
    async def stats_overview(hours: int = Query(default=24, ge=1, le=720)) -> dict:
        return await service.stats_overview(hours)

    @router.get("/stats/timeseries")
    async def stats_timeseries(hours: int = Query(default=24, ge=1, le=720)) -> dict:
        return await service.stats_timeseries(hours)

    @router.get("/stats/tools")
    async def stats_tools(
        hours: int = Query(default=24, ge=1, le=720),
        limit: int = Query(default=10, ge=1, le=100),
    ) -> dict:
        return await service.stats_tools(hours, limit)

    @router.get("/stats/agents")
    async def stats_agents(
        hours: int = Query(default=24, ge=1, le=720),
        limit: int = Query(default=10, ge=1, le=100),
    ) -> dict:
        return await service.stats_agents(hours, limit)

    @router.get("/stats/llm")
    async def stats_llm(hours: int = Query(default=24, ge=1, le=720)) -> dict:
        return await service.stats_llm(hours)

    @router.get("/stats/events")
    async def stats_events(hours: int = Query(default=24, ge=1, le=720)) -> dict:
        return await service.stats_events(hours)

    @router.get("/runs/recent")
    async def runs_recent(limit: int = Query(default=20, ge=1, le=100)) -> dict:
        return await service.recent_runs(limit)

    return router
