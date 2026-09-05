"""Compact, stable SSE projection for the Runtime ingress boundary."""

from __future__ import annotations

import json
from collections.abc import AsyncIterable, AsyncIterator
from typing import Any
from .public_thinking import PublicThinkingStream

_TERMINAL_EVENTS = {"answer.completed", "answer.failed"}
_RECOVERABLE_SESSION_ERROR_CODES = {
    "RUNTIME_SESSION_NOT_FOUND",
    "RUNTIME_SESSION_SCOPE_MISMATCH",
}
_PUBLIC_ERROR_CODES = _RECOVERABLE_SESSION_ERROR_CODES | {
    "WORKER_UNAVAILABLE", "WORKER_TIMEOUT", "ARTIFACT_TOOL_NOT_INVOKED",
    "ARTIFACT_OUTPUT_MISSING", "ARTIFACT_PUBLISH_INCOMPLETE",
    "QWENPAW_TASK_CANCELLED", "QWENPAW_DOOM_LOOP_STOP",
}


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("text", "delta", "content", "message"):
            result = _text(value.get(key))
            if result:
                return result
        return ""
    if isinstance(value, list):
        return "".join(_text(item) for item in value)
    return str(getattr(value, "text", "") or "")


def _delta(previous: str, current: str) -> str:
    if previous and current.startswith(previous):
        return current[len(previous) :]
    if current == previous:
        return ""
    return current


class CompactEventProjector:
    """Project native 2.1 events into the small Runtime-owned event set."""

    def __init__(self, runtime_task_id: str) -> None:
        self.runtime_task_id = runtime_task_id
        self._snapshots: dict[tuple[str, str], str] = {}
        self._message_stream_types: dict[str, str] = {}
        self._terminal = False
        self._active_message_id = ""

    def project(self, raw_event: dict[str, Any]) -> list[dict[str, Any]]:
        phases = []
        if not self._terminal and isinstance(raw_event, dict) and raw_event.get('object') == 'message':
            message_id = str(raw_event.get('msg_id') or raw_event.get('message_id') or raw_event.get('id') or '')
            kind = str(raw_event.get('type') or '').lower()
            previous = self._active_message_id
            if previous and kind in {'reasoning', 'thinking'} and message_id == previous:
                self._active_message_id = ''
            elif previous and message_id != previous and kind in {'message', 'plugin_call', 'plugin_call_output', 'reasoning', 'thinking'}:
                text = self._snapshots.get(('answer.chunk', previous), '')
                if text:
                    phases.append({'event': 'answer.phase', 'message_id': previous, 'text': text})
                self._active_message_id = ''
            if kind == 'message' and message_id:
                self._active_message_id = message_id
        return phases + self._project(raw_event)

    def _project(self, raw_event: dict[str, Any]) -> list[dict[str, Any]]:
        if self._terminal or not isinstance(raw_event, dict):
            return []
        event_name = str(
            raw_event.get("event") or raw_event.get("event_type") or ""
        ).strip()
        status = str(raw_event.get("status") or "").strip().lower()
        obj = str(raw_event.get("object") or "").strip().lower()

        if event_name in _TERMINAL_EVENTS:
            return [self._terminal_event(event_name)]
        if event_name == "status.changed":
            payload = raw_event.get("payload")
            payload = payload if isinstance(payload, dict) else raw_event
            raw_status = str(payload.get("status") or "").strip().lower()
            if raw_status in {"cancelled", "failed", "timeout"}:
                return [self._terminal_event("answer.failed", raw_status)]
            aliases = {
                "accepted": ("task.accepted", "已收到问题"),
                "preparing": ("answer.preparing", "正在准备回答"),
                "answering": ("answer.generating", "正在生成回答"),
            }
            if raw_status in aliases:
                normalized, message = aliases[raw_status]
                return [
                    {
                        "event": "status.changed",
                        "status": normalized,
                        "message": message,
                    }
                ]
            return []
        if event_name in {"answer.thinking", "answer.chunk"}:
            content = _text(raw_event)
            return [{"event": event_name, "text": content}] if content else []
        if event_name in {"message", "message_delta", "delta"}:
            content = _text(raw_event.get("delta") or raw_event.get("text"))
            return [{"event": "answer.chunk", "text": content}] if content else []
        if event_name in {"error", "rate_limited"} or "error" in raw_event:
            error = raw_event.get("error")
            error = error if isinstance(error, dict) else {}
            error_code = str(error.get("code") or "")
            return [
                self._terminal_event(
                    "answer.failed",
                    error_code=error_code,
                )
            ]

        if obj == "response":
            if status in {"completed", "done", "success"}:
                return [self._terminal_event("answer.completed")]
            if status in {"failed", "error", "cancelled"}:
                return [self._terminal_event("answer.failed", status)]
            return []
        if obj not in {"message", "content"}:
            return []

        message_id = str(
            raw_event.get("msg_id")
            or raw_event.get("message_id")
            or raw_event.get("id")
            or ""
        )
        declared_stream_type = str(
            raw_event.get("type") or "message"
        ).strip().lower()
        if obj == "message" and message_id:
            self._message_stream_types[message_id] = declared_stream_type
        stream_type = (
            self._message_stream_types.get(message_id, declared_stream_type)
            if obj == "content"
            else declared_stream_type
        )
        if stream_type not in {"message", "reasoning", "thinking", "text"}:
            return []
        is_thinking = stream_type in {"reasoning", "thinking"}
        event = "answer.thinking" if is_thinking else "answer.chunk"
        if not message_id:
            message_id = stream_type
        raw_delta = raw_event.get("delta")
        if isinstance(raw_delta, bool):
            raw_content = raw_event.get("text") or raw_event.get("content")
        else:
            raw_content = raw_delta or raw_event.get("content") or raw_event.get("text")
        current = _text(raw_content)
        key = (event, message_id)
        previous = self._snapshots.get(key, "")
        if raw_event.get("delta"):
            chunk = current
            self._snapshots[key] = previous + chunk
        else:
            chunk = _delta(previous, current)
            self._snapshots[key] = current
        if not chunk:
            return []
        payload = {"event": event, "text": chunk}
        if not is_thinking and self._active_message_id == message_id:
            payload["message_id"] = message_id
        return [payload]

    def finish(self) -> list[dict[str, Any]]:
        if self._terminal:
            return []
        return [self._terminal_event("answer.failed")]

    def _terminal_event(
        self,
        event: str,
        raw_status: str = "",
        error_code: str = "",
    ) -> dict[str, Any]:
        self._terminal = True
        if event == "answer.completed":
            return {
                "event": event,
                "status": "completed",
                "message": "回答完成",
            }
        message = "已停止生成" if raw_status == "cancelled" else "回答生成失败"
        failed = {
            "event": "answer.failed",
            "status": "failed",
            "message": message,
        }
        if raw_status == "cancelled":
            error_code = "QWENPAW_TASK_CANCELLED"
        elif raw_status == "timeout":
            error_code = "WORKER_TIMEOUT"
        if error_code in _PUBLIC_ERROR_CODES:
            failed["error_code"] = error_code
        return failed


def _decode_sse_block(block: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in block.splitlines():
        if not line.startswith("data:"):
            continue
        try:
            value = json.loads(line.removeprefix("data:").strip())
        except json.JSONDecodeError:
            value = {"error": "malformed upstream event"}
        if isinstance(value, dict):
            events.append(value)
    return events


async def project_sse_stream(
    source: AsyncIterable[str],
    runtime_task_id: str,
) -> AsyncIterator[str]:
    """Project an upstream SSE stream while preserving disconnect semantics."""
    projector = CompactEventProjector(runtime_task_id)
    public_thinking = PublicThinkingStream()
    yield _encode(
        {
            "event": "status.changed",
            "status": "task.accepted",
            "message": "已收到问题",
        }
    )
    buffer = ""
    async for item in source:
        buffer += str(item)
        while "\n\n" in buffer:
            block, buffer = buffer.split("\n\n", 1)
            for raw_event in _decode_sse_block(block):
                for event in projector.project(raw_event):
                    for public in public_thinking.project(event):
                        yield _encode(public)
    if buffer.strip():
        for raw_event in _decode_sse_block(buffer):
            for event in projector.project(raw_event):
                for public in public_thinking.project(event):
                    yield _encode(public)
    for event in projector.finish():
        for public in public_thinking.project(event):
            yield _encode(public)


def _encode(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
