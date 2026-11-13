"""Authenticated, agent-scoped HTTP ingress owned by the plugin."""

from __future__ import annotations

from collections.abc import AsyncGenerator
import os
from typing import Any

from fastapi import APIRouter, Body, Header, HTTPException, Query, Request
from starlette.responses import StreamingResponse

from qwenpaw.app.agent_context import get_agent_for_request
from qwenpaw.schemas import AgentRequest

from .auth import require_service_identity
from .events import project_sse_stream
from .production_guard import require_production_readiness
from .release import BANK_RUNTIME_PLUGIN_VERSION
from .remote_state import RemoteStateError, execution_from_request, execution_run_key


def _trusted_agent_id(
    authorization: str | None,
    header_agent_id: str | None,
    path_agent_id: str | None,
) -> str:
    trusted = require_service_identity(authorization, header_agent_id)
    if path_agent_id is not None and path_agent_id != trusted:
        raise HTTPException(
            status_code=401,
            detail="Agent-scoped service identity does not match",
        )
    require_production_readiness()
    return trusted


def _required_text(payload: dict[str, Any], key: str) -> str:
    value = str(payload.get(key) or "").strip()
    if not value:
        raise HTTPException(status_code=400, detail=f"{key} is required")
    return value


def _validated_chat_request(
    payload: dict[str, Any],
) -> tuple[str, str, str, AgentRequest]:
    runtime_task_id = _required_text(payload, "runtime_task_id")
    session_id = _required_text(payload, "session_id")
    user_id = _required_text(payload, "user_id")
    if payload.get("channel") != "bank-runtime":
        raise HTTPException(
            status_code=400,
            detail="channel must be bank-runtime",
        )
    if payload.get('runtime_config_release') is not None and (
            not payload.get('runtime_worker_node_id') or payload['runtime_worker_node_id'] != os.environ.get('QWENPAW_NODE_ID')):
        raise HTTPException(status_code=409, detail='Configuration worker node mismatch')
    sandbox_context = payload.get("sandbox_context")
    if sandbox_context is not None and (not isinstance(sandbox_context, dict)
            or str(sandbox_context.get("task_id") or "") != runtime_task_id):
        raise HTTPException(status_code=400, detail="sandbox_context.task_id must match runtime_task_id")
    messages = payload.get("input")
    execution = payload.get("runtime_execution")
    resume = isinstance(execution, dict) and execution.get("resume") is True
    if resume:
        if messages != []:
            raise HTTPException(status_code=400, detail="resume requires an empty input list")
        return runtime_task_id, session_id, user_id, AgentRequest(**payload)
    if not isinstance(messages, list) or len(messages) != 1:
        raise HTTPException(
            status_code=400,
            detail="exactly one current user message is required",
        )
    message = messages[0]
    if (
        not isinstance(message, dict)
        or str(message.get("role") or "").lower() != "user"
    ):
        raise HTTPException(
            status_code=400,
            detail="the current message must have role user",
        )
    content = message.get("content")
    if not isinstance(content, list) or not content:
        raise HTTPException(
            status_code=400,
            detail="the current user message must have content",
        )
    return runtime_task_id, session_id, user_id, AgentRequest(**payload)


def build_ingress_router() -> APIRouter:
    router = APIRouter()
    from .classification_operations import build_classification_router
    router.include_router(build_classification_router())
    from .content_review import build_content_review_router
    router.include_router(build_content_review_router())
    from .background_analysis import build_background_analysis_router
    router.include_router(build_background_analysis_router())
    from .method_catalog import catalog_router
    router.include_router(catalog_router())
    from .context_summary import build_context_summary_router
    router.include_router(build_context_summary_router())
    from .context_budget import build_context_budget_router
    router.include_router(build_context_budget_router())
    from .config_release import build_config_release_router
    router.include_router(build_config_release_router())

    @router.get("/health")
    @router.get("/agents/{agent_id}/health")
    async def health(
        agent_id: str | None = None,
        authorization: str | None = Header(default=None),
        header_agent_id: str | None = Header(
            default=None,
            alias="X-Agent-Id",
        ),
    ) -> dict[str, Any]:
        _trusted_agent_id(
            authorization,
            header_agent_id,
            agent_id,
        )
        return {
            "status": "ok",
            "channel": "bank-runtime",
            "plugin_version": BANK_RUNTIME_PLUGIN_VERSION,
        }

    @router.post("/chat")
    @router.post("/agents/{agent_id}/chat")
    async def chat(
        request: Request,
        payload: dict[str, Any] = Body(...),
        agent_id: str | None = None,
        authorization: str | None = Header(default=None),
        header_agent_id: str | None = Header(
            default=None,
            alias="X-Agent-Id",
        ),
    ) -> StreamingResponse:
        trusted = _trusted_agent_id(
            authorization,
            header_agent_id,
            agent_id,
        )
        runtime_task_id, session_id, user_id, agent_request = _validated_chat_request(
            payload
        )
        try:
            execution = execution_from_request(agent_request, trusted)
        except RemoteStateError as exc:
            raise HTTPException(status_code=400, detail={"code": exc.error_code}) from exc
        workspace = await get_agent_for_request(request, trusted)
        channel = await workspace.channel_manager.get_channel("bank-runtime")
        if channel is None:
            raise HTTPException(
                status_code=503,
                detail="Bank Runtime channel is unavailable",
            )
        if execution is not None:
            from .execution_stream import open_execution_stream
            from .remote_state import RemoteStateClient
            try:
                stream = await open_execution_stream(RemoteStateClient(execution),
                    lambda: project_sse_stream(channel.stream_one(agent_request), runtime_task_id,
                        str(getattr(agent_request, 'trace_id', '') or '')),
                    after_sequence=payload.get('runtime_event_cursor', 0))
            except RemoteStateError as exc:
                raise HTTPException(409 if exc.http_status == 409 else 503, {'code': exc.error_code}) from exc
            return StreamingResponse(stream, media_type='text/event-stream',
                headers={'Cache-Control': 'no-cache', 'Connection': 'keep-alive'})
        else:
            chat_record = await workspace.chat_manager.get_or_create_chat(session_id, user_id, "bank-runtime")
            run_key = chat_record.id
        queue, _ = await workspace.task_tracker.attach_or_start(
            run_key,
            agent_request,
            lambda item: project_sse_stream(
                channel.stream_one(item),
                runtime_task_id,
                str(getattr(agent_request, "trace_id", "") or ""),
            ),
            owner=workspace,
        )

        async def event_generator() -> AsyncGenerator[str, None]:
            stream = workspace.task_tracker.stream_from_queue(
                queue,
                run_key,
            )
            try:
                async for item in stream:
                    yield item
            finally:
                await stream.aclose()

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )

    @router.post("/chat/stop")
    @router.post("/agents/{agent_id}/chat/stop")
    async def stop(
        request: Request,
        chat_id: str = Query(...),
        payload: dict[str, Any] = Body(...),
        agent_id: str | None = None,
        authorization: str | None = Header(default=None),
        header_agent_id: str | None = Header(
            default=None,
            alias="X-Agent-Id",
        ),
    ) -> dict[str, Any]:
        trusted = _trusted_agent_id(
            authorization,
            header_agent_id,
            agent_id,
        )
        _required_text(payload, "runtime_task_id")
        session_id = _required_text(payload, "session_id")
        if chat_id != session_id:
            raise HTTPException(
                status_code=400,
                detail="chat_id must match session_id",
            )
        workspace = await get_agent_for_request(request, trusted)
        if payload.get("runtime_execution") is not None:
            try:
                resolved = execution_run_key(trusted, payload["runtime_task_id"], session_id,
                    _required_text(payload, "user_id"), payload["runtime_execution"])
            except RemoteStateError as exc:
                raise HTTPException(status_code=400, detail={"code": exc.error_code}) from exc
        else:
            resolved = await workspace.chat_manager.get_chat_id_by_session(session_id=session_id, channel="bank-runtime")
        if payload.get('runtime_execution') is not None:
            from .execution_stream import stop_local_execution
            stopped = bool(resolved and stop_local_execution(resolved))
        else:
            stopped = bool(resolved and await workspace.task_tracker.request_stop(resolved))
        return {
            "runtime_task_id": payload["runtime_task_id"],
            "stopped": stopped,
            "stop_status": "stopped" if stopped else "already_stopped",
        }

    return router
