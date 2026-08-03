# -*- coding: utf-8 -*-
"""Console APIs: push messages, chat, and file upload for chat."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from pathlib import Path
from typing import AsyncGenerator, Union

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel
from starlette.responses import StreamingResponse

from agentscope_runtime.engine.schemas.agent_schemas import AgentRequest
from ...utils.logging import LOG_FILE_PATH
from ..agent_context import get_agent_for_request
from ..runner.title_generator import generate_and_update_title
from ..utils import check_upload_size


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/console", tags=["console"])


class MarkInboxReadRequest(BaseModel):
    event_ids: list[str] = []
    all: bool = False


MAX_DEBUG_LOG_LINES = 1000
RUNTIME_CHANNEL_META_KEYS = (
    "conversation_id",
    "runtime_task_id",
    "trace_id",
    "runtime_execution_mode",
    "runtime_response_mode",
    "runtime_generation_controls",
    "runtime_datetime_context",
    "runtime_latency_marks",
    "runtime_constraints",
    "execution_sandbox",
    "policy_search_context",
    "identity_json",
    "runtime_governance",
    "runtime_context",
    "runtime_tool_gateway",
    "attachments_manifest",
    "sandbox_context",
    "personal_skills_catalog",
    "personal_skills_access_manifest",
)


def _safe_filename(name: str) -> str:
    """Safe basename, alphanumeric/./-/_, max 200 chars."""
    base = Path(name).name if name else "file"
    return re.sub(r"[^\w.\-]", "_", base)[:200] or "file"


def _extract_placeholder_name(content_parts: list) -> tuple[str, str]:
    """Return ``(placeholder_name, first_user_text)`` for a new chat.

    The placeholder name shows up in the session drawer immediately while a
    background task asks the model for a real title. Content shapes match
    ``channels/base.py::_extract_chat_name``: dict blocks like
    ``{"type": "text", "text": "..."}``, raw strings, and objects with a
    ``.text`` attribute. Anything else (audio/image/file blocks) is treated
    as media and gets the generic "Media Message" placeholder.
    """
    if not content_parts:
        return "New Chat", ""
    content = content_parts[0]
    if not content:
        return "Media Message", ""
    if isinstance(content, str):
        first_text = content
    elif isinstance(content, dict):
        text = content.get("text", "")
        first_text = text if isinstance(text, str) else ""
    elif hasattr(content, "text"):
        first_text = content.text or ""
    else:
        first_text = ""
    if not first_text:
        return "Media Message", ""
    return first_text[:10], first_text


def _extract_session_and_payload(request_data: Union[AgentRequest, dict]):
    """Extract run_key (ChatSpec.id), session_id, and native payload.

    run_key must be ChatSpec.id (chat_id) so it matches list_chats/get_chat.
    """
    if isinstance(request_data, AgentRequest):
        channel_id = getattr(request_data, "channel", None) or "console"
        sender_id = request_data.user_id or "default"
        session_id = request_data.session_id or "default"
        content_parts = (
            list(request_data.input[0].content) if request_data.input else []
        )
    else:
        channel_id = request_data.get("channel", "console")
        sender_id = request_data.get("user_id", "default")
        session_id = request_data.get("session_id", "default")
        input_data = request_data.get("input", [])
        content_parts = []
        for content_part in input_data:
            if hasattr(content_part, "content"):
                content_parts.extend(list(content_part.content or []))
            elif isinstance(content_part, dict) and "content" in content_part:
                content_parts.extend(content_part["content"] or [])

    channel_meta = {
        "session_id": session_id,
        "user_id": sender_id,
    }
    if isinstance(request_data, dict):
        for key in RUNTIME_CHANNEL_META_KEYS:
            if key in request_data:
                channel_meta[key] = request_data[key]
    else:
        for key in RUNTIME_CHANNEL_META_KEYS:
            value = getattr(request_data, key, None)
            if value is not None:
                channel_meta[key] = value

    native_payload = {
        "channel_id": channel_id,
        "sender_id": sender_id,
        "content_parts": content_parts,
        "meta": channel_meta,
    }
    return native_payload


def _is_runtime_native_payload(native_payload: dict) -> bool:
    return (
        isinstance(native_payload, dict)
        and native_payload.get("channel_id") == "bank-runtime"
    )


def _is_runtime_terminal_sse(event_data: str) -> bool:
    for raw_line in str(event_data).splitlines():
        line = raw_line.strip()
        if not line.startswith("data:"):
            continue
        try:
            payload = json.loads(line.removeprefix("data:").strip())
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        event = str(payload.get("event") or payload.get("event_type") or "").strip()
        status = str(payload.get("status") or "").strip()
        if event in {
            "completed",
            "done",
            "success",
            "agent.completed",
            "answer.completed",
            "failed",
            "error",
            "agent.failed",
            "answer.failed",
        }:
            return True
        if payload.get("object") == "response" and status in {
            "completed",
            "failed",
            "error",
        }:
            return True
    return False


def _runtime_missing_terminal_sse(native_payload: dict) -> str:
    del native_payload
    return (
        "data: "
        f"{json.dumps({'event': 'answer.failed', 'status': 'failed', 'message': '回答生成失败'}, ensure_ascii=False)}"
        "\n\n"
    )


def _runtime_invalid_sandbox_sse() -> str:
    return (
        "data: "
        f"{json.dumps({'event': 'answer.failed', 'status': 'failed', 'message': '回答生成失败'}, ensure_ascii=False)}"
        "\n\n"
    )


def _runtime_task_ids(meta: dict) -> tuple[str, str]:
    runtime_task_id = str(meta.get("runtime_task_id") or "").strip()
    sandbox_context = meta.get("sandbox_context")
    sandbox_task_id = (
        str(sandbox_context.get("task_id") or "").strip()
        if isinstance(sandbox_context, dict)
        else ""
    )
    return runtime_task_id, sandbox_task_id


async def _stream_runtime_console_events(
    console_channel,
    native_payload: dict,
) -> AsyncGenerator[str, None]:
    runtime_request = (
        isinstance(native_payload, dict)
        and native_payload.get("channel_id") == "bank-runtime"
    )
    meta = native_payload.get("meta") if isinstance(native_payload, dict) else {}
    if not isinstance(meta, dict):
        meta = {}
    runtime_task_id, sandbox_task_id = _runtime_task_ids(meta)
    if runtime_request and (
        not runtime_task_id
        or not sandbox_task_id
        or runtime_task_id != sandbox_task_id
    ):
        await _cleanup_runtime_task_files_many(
            sandbox_task_id,
            runtime_task_id,
        )
        yield _runtime_invalid_sandbox_sse()
        return

    lifetime_reservation = None
    if runtime_request:
        from ...agents.tools import runtime_sandbox_oss

        try:
            lifetime_reservation = (
                runtime_sandbox_oss._DEFAULT_TASK_ATTACHMENT_CACHE.reserve_task_io(
                    runtime_task_id,
                )
            )
        except Exception:
            await _cleanup_runtime_task_files(runtime_task_id)
            yield _runtime_invalid_sandbox_sse()
            return
    try:
        terminal_seen = False
        async for event_data in console_channel.stream_one(native_payload):
            if _is_runtime_terminal_sse(event_data):
                terminal_seen = True
            yield event_data
        if not terminal_seen:
            yield _runtime_missing_terminal_sse(native_payload)
    finally:
        if lifetime_reservation is not None:
            lifetime_reservation.release()
        if runtime_request and runtime_task_id:
            await _cleanup_runtime_task_files(runtime_task_id)


async def _cleanup_runtime_task_files_many(*task_ids: str) -> None:
    seen: set[str] = set()
    for task_id in task_ids:
        normalized = str(task_id or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        await _cleanup_runtime_task_files(normalized)


async def _cleanup_runtime_task_files(runtime_task_id: str) -> None:
    from ...agents.tools import runtime_sandbox_oss

    cleanup = asyncio.create_task(
        asyncio.to_thread(
            runtime_sandbox_oss._DEFAULT_TASK_ATTACHMENT_CACHE.cleanup_task,
            runtime_task_id,
        ),
    )
    try:
        await asyncio.shield(cleanup)
    except asyncio.CancelledError:
        await cleanup
        raise
    except Exception:
        logger.exception(
            "Failed to clean Runtime task attachment cache task_id=%s",
            runtime_task_id,
        )


def _tail_text_file(
    path: Path,
    *,
    lines: int = 200,
    max_bytes: int = 512 * 1024,
) -> str:
    """Read the last N lines from a text file with bounded memory."""
    path = Path(path)
    if not path.exists() or not path.is_file():
        return ""
    try:
        size = path.stat().st_size
        if size == 0:
            return ""
        with open(path, "rb") as f:
            if size <= max_bytes:
                data = f.read()
            else:
                f.seek(max(size - max_bytes, 0))
                data = f.read()
        text = data.decode("utf-8", errors="replace")
        return "\n".join(text.splitlines()[-lines:])
    except Exception:
        logger.exception("Failed to read backend debug log file")
        return ""


@router.post(
    "/chat",
    status_code=200,
    summary="Chat with console (streaming response)",
    description="Agent API Request Format. See runtime.agentscope.io. "
    "Use body.reconnect=true to attach to a running stream.",
)
async def post_console_chat(
    request_data: dict,
    request: Request,
) -> StreamingResponse:
    """Stream agent response. Run continues in background after disconnect.
    Stop via POST /console/chat/stop. Reconnect with body.reconnect=true.
    """
    workspace = await get_agent_for_request(request)
    console_channel = await workspace.channel_manager.get_channel("console")
    if console_channel is None:
        raise HTTPException(
            status_code=503,
            detail="Channel Console not found",
        )
    try:
        native_payload = _extract_session_and_payload(request_data)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    session_id = console_channel.resolve_session_id(
        sender_id=native_payload["sender_id"],
        channel_meta=native_payload["meta"],
    )
    # Runtime owns conversation history and task tracking.  Do not mirror a
    # Runtime-managed request into QwenPaw's chats.json, title generator, or
    # reconnect tracker: those stores live in the shared Agent workspace.
    if _is_runtime_native_payload(native_payload):
        return StreamingResponse(
            _stream_runtime_console_events(console_channel, native_payload),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )
    name, first_text = _extract_placeholder_name(
        native_payload["content_parts"],
    )
    chat = await workspace.chat_manager.get_or_create_chat(
        session_id,
        native_payload["sender_id"],
        native_payload["channel_id"],
        name=name,
    )
    tracker = workspace.task_tracker

    # Kick off an LLM-backed title generation in the background when the chat
    # was just created with the truncated placeholder. This runs detached so
    # the streaming response is never blocked by title generation latency.
    if first_text and chat.name == name:
        asyncio.create_task(
            generate_and_update_title(
                workspace=workspace,
                chat_id=chat.id,
                user_message=first_text,
                placeholder_name=name,
            ),
        )

    is_reconnect = False
    if isinstance(request_data, dict):
        is_reconnect = request_data.get("reconnect") is True

    if is_reconnect:
        queue = await tracker.attach(chat.id)
        if queue is None:
            return
    else:
        queue, _ = await tracker.attach_or_start(
            chat.id,
            native_payload,
            console_channel.stream_one,
        )

    async def event_generator() -> AsyncGenerator[str, None]:
        # Hold iterator so finally can aclose(); guarantees stream_from_queue's
        # finally (detach_subscriber) on client abort / generator teardown.
        stream_it = tracker.stream_from_queue(queue, chat.id)
        runtime_request = _is_runtime_native_payload(native_payload)
        terminal_seen = False
        try:
            try:
                async for event_data in stream_it:
                    if runtime_request and _is_runtime_terminal_sse(event_data):
                        terminal_seen = True
                    yield event_data
                if runtime_request and not terminal_seen:
                    yield _runtime_missing_terminal_sse(native_payload)
            except Exception as e:
                logger.exception("Console chat stream error")
                yield f"data: {json.dumps({'error': str(e)})}\n\n"
        finally:
            await stream_it.aclose()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


@router.post(
    "/chat/stop",
    status_code=200,
    summary="Stop running console chat",
)
async def post_console_chat_stop(
    request: Request,
    chat_id: str = Query(..., description="Chat id (ChatSpec.id) to stop"),
) -> dict:
    """Stop the running chat. Only stops when called."""
    logger.debug("[STOP API] Received stop request for chat_id=%s", chat_id)
    workspace = await get_agent_for_request(request)

    # Try to stop with the provided chat_id first
    logger.debug(
        "[STOP API] Got workspace, calling task_tracker.request_stop...",
    )
    stopped = await workspace.task_tracker.request_stop(chat_id)

    # If not found, the chat_id might be a session_id (timestamp)
    # Try to resolve it to the actual chat UUID
    if not stopped:
        logger.debug(
            "[STOP API] chat_id not found in tracker, trying to resolve "
            "from session_id...",
        )
        chat_manager = getattr(workspace.runner, "_chat_manager", None)
        if chat_manager:
            resolved_chat_id = await chat_manager.get_chat_id_by_session(
                session_id=chat_id,
                channel="console",
            )
            if resolved_chat_id:
                logger.debug(
                    "[STOP API] Resolved session_id=%s to chat_id=%s",
                    chat_id[:12] if len(chat_id) >= 12 else chat_id,
                    resolved_chat_id,
                )
                stopped = await workspace.task_tracker.request_stop(
                    resolved_chat_id,
                )

    logger.debug(
        "[STOP API] task_tracker.request_stop returned: stopped=%s",
        stopped,
    )
    return {"stopped": stopped}


@router.post("/upload", response_model=dict, summary="Upload file for chat")
async def post_console_upload(
    request: Request,
    file: UploadFile = File(..., description="File to attach"),
) -> dict:
    """Save to console channel media_dir."""

    workspace = await get_agent_for_request(request)
    console_channel = await workspace.channel_manager.get_channel("console")
    if console_channel is None:
        raise HTTPException(
            status_code=503,
            detail="Channel Console not found",
        )
    media_dir = console_channel.media_dir
    media_dir.mkdir(parents=True, exist_ok=True)
    data = await file.read()
    check_upload_size(data)
    safe_name = _safe_filename(file.filename or "file")
    stored_name = f"{uuid.uuid4().hex}_{safe_name}"

    path = (media_dir / stored_name).resolve()
    path.write_bytes(data)
    return {
        "url": path,
        "file_name": safe_name,
        "size": len(data),
    }


@router.get(
    "/debug/backend-logs",
    response_model=dict,
    summary="Read backend daemon logs for debug page",
)
async def get_backend_debug_logs(
    lines: int = Query(
        200,
        ge=20,
        le=MAX_DEBUG_LOG_LINES,
        description="Number of trailing log lines to return",
    ),
) -> dict:
    """Return the tail of the project log file for the debug UI."""
    log_path = LOG_FILE_PATH.resolve()
    try:
        st = log_path.stat()
        return {
            "path": str(log_path),
            "exists": True,
            "lines": lines,
            "updated_at": st.st_mtime,
            "size": st.st_size,
            "content": _tail_text_file(log_path, lines=lines),
        }
    except FileNotFoundError:
        return {
            "path": str(log_path),
            "exists": False,
            "lines": lines,
            "updated_at": None,
            "size": 0,
            "content": "",
        }


@router.get("/push-messages")
async def get_push_messages(
    session_id: str | None = Query(None, description="Optional session id"),
):
    """
    Return pending push messages and ALL approval requests.

    Messages:
    - With session_id: consumed messages for that session
    - Without session_id: recent messages (all sessions, last 60s)

    Approvals:
    - Always returns ALL pending approvals across all sessions
    - Frontend filters by current session_id for display
    - Includes session_id in each approval for filtering
    """
    from ..console_push_store import get_recent, take
    from ..approvals import get_approval_service

    # Get messages (session-specific or global)
    if session_id:
        messages = await take(session_id)
    else:
        messages = await get_recent()

    # Get ALL pending approvals (not filtered by session)
    approval_svc = get_approval_service()
    # pylint: disable=protected-access
    async with approval_svc._lock:
        all_pending = list(approval_svc._pending.values())

    # Serialize approval data with root_session_id for frontend filtering
    approvals_data = [
        {
            "request_id": p.request_id,
            "session_id": p.session_id,
            "root_session_id": p.root_session_id,
            "owner_agent_id": p.owner_agent_id,
            "agent_id": p.agent_id,
            "tool_name": p.tool_name,
            "severity": p.severity,
            "findings_count": p.findings_count,
            "findings_summary": p.result_summary,
            "tool_params": p.extra.get("tool_call", {}).get("input", {}),
            "created_at": p.created_at,
            "timeout_seconds": p.timeout_seconds,
        }
        for p in all_pending
    ]

    return {"messages": messages, "pending_approvals": approvals_data}


@router.get("/inbox/events")
async def get_inbox_events(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    source_type: str | None = Query(None),
    status: str | None = Query(None),
    agent_id: str | None = Query(None),
    unread_only: bool = Query(False),
):
    from ..inbox_store import list_events

    events = await list_events(
        limit=limit,
        offset=offset,
        source_type=source_type,
        status=status,
        agent_id=agent_id,
        unread_only=unread_only,
    )
    return {"events": events}


@router.post("/inbox/read")
async def post_mark_inbox_read(payload: MarkInboxReadRequest):
    from ..inbox_store import mark_all_read, mark_read

    if payload.all:
        updated = await mark_all_read()
    else:
        updated = await mark_read(payload.event_ids)
    return {"updated": updated}


@router.delete("/inbox/events/{event_id}")
async def delete_inbox_event(event_id: str):
    from ..inbox_store import delete_event
    from ..inbox_trace_store import delete_trace

    deleted, run_id, run_id_still_referenced = await delete_event(event_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="event not found")
    trace_deleted = False
    if run_id and not run_id_still_referenced:
        trace_deleted = await delete_trace(run_id)
    return {
        "deleted": True,
        "trace_deleted": trace_deleted,
        "run_id": run_id,
    }


@router.get("/inbox/traces/{run_id}")
async def get_inbox_trace(run_id: str):
    from ..inbox_trace_store import get_trace

    trace = await get_trace(run_id)
    if trace is None:
        raise HTTPException(status_code=404, detail="trace not found")
    return trace
