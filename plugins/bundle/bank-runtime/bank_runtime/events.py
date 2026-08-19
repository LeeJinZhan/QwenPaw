"""Compact, stable SSE projection for the Runtime ingress boundary."""

from __future__ import annotations

import json
from collections.abc import AsyncIterable, AsyncIterator
from typing import Any

_TERMINAL_EVENTS = {"answer.completed", "answer.failed"}


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
        self._terminal = False

    def project(self, raw_event: dict[str, Any]) -> list[dict[str, Any]]:
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
            return [self._terminal_event("answer.failed")]

        if obj == "response":
            if status in {"completed", "done", "success"}:
                return [self._terminal_event("answer.completed")]
            if status in {"failed", "error", "cancelled"}:
                return [self._terminal_event("answer.failed", status)]
            return []
        if obj not in {"message", "content"}:
            return []

        stream_type = str(raw_event.get("type") or "message").strip().lower()
        is_thinking = stream_type in {"reasoning", "thinking"}
        event = "answer.thinking" if is_thinking else "answer.chunk"
        message_id = str(
            raw_event.get("msg_id")
            or raw_event.get("message_id")
            or raw_event.get("id")
            or stream_type
        )
        current = _text(
            raw_event.get("delta") or raw_event.get("content") or raw_event.get("text")
        )
        key = (event, message_id)
        previous = self._snapshots.get(key, "")
        if raw_event.get("delta"):
            chunk = current
            self._snapshots[key] = previous + chunk
        else:
            chunk = _delta(previous, current)
            self._snapshots[key] = current
        return [{"event": event, "text": chunk}] if chunk else []

    def finish(self) -> list[dict[str, Any]]:
        if self._terminal:
            return []
        return [self._terminal_event("answer.failed")]

    def _terminal_event(
        self,
        event: str,
        raw_status: str = "",
    ) -> dict[str, Any]:
        self._terminal = True
        if event == "answer.completed":
            return {
                "event": event,
                "status": "completed",
                "message": "回答完成",
            }
        message = "已停止生成" if raw_status == "cancelled" else "回答生成失败"
        return {
            "event": "answer.failed",
            "status": "failed",
            "message": message,
        }


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
                    yield _encode(event)
    if buffer.strip():
        for raw_event in _decode_sse_block(buffer):
            for event in projector.project(raw_event):
                yield _encode(event)
    for event in projector.finish():
        yield _encode(event)


def _encode(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
