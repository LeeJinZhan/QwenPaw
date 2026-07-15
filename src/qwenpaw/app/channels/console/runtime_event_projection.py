# -*- coding: utf-8 -*-
"""Project native AgentScope events into the public Runtime event stream."""

from __future__ import annotations

import json
import time
from typing import Any

_SUCCESS_STATES = {"completed", "complete", "success", "succeeded", "done"}
_FAILURE_STATES = {"failed", "failure", "error", "rejected", "incomplete"}
_CANCEL_STATES = {"cancelled", "canceled", "cancel", "aborted"}
_VISIBLE_MESSAGE_TYPES = {"message", "assistant"}
_VISIBLE_TEXT_TYPES = {"text", "outputtext"}
_REASONING_MESSAGE_TYPES = {"reasoning", "thinking"}
_SAFE_FAILURE_MESSAGES = {
    "附件读取失败",
    "回答生成失败",
    "任务已取消",
}
DEFAULT_MIN_CHUNK_CHARS = 256
DEFAULT_MAX_CHUNK_CHARS = 512
DEFAULT_MAX_CHUNK_DELAY_SECONDS = 0.05
STATUS_ANSWER_GENERATING = "answer.generating"
STATUS_ANSWER_COMPLETED = "completed"
STATUS_ANSWER_FAILED = "failed"


def _token(value: Any) -> str:
    value = getattr(value, "value", value)
    return "".join(
        character
        for character in str(value or "").strip().lower()
        if character.isalnum()
    )


def _event_mapping(event: Any) -> dict[str, Any]:
    if isinstance(event, dict):
        return event
    if hasattr(event, "model_dump"):
        try:
            data = event.model_dump(mode="python")
            if isinstance(data, dict):
                return data
        except Exception:  # pylint: disable=broad-exception-caught
            pass
    if hasattr(event, "dict"):
        try:
            data = event.dict()
            if isinstance(data, dict):
                return data
        except Exception:  # pylint: disable=broad-exception-caught
            pass
    if hasattr(event, "model_dump_json"):
        try:
            data = json.loads(event.model_dump_json())
            if isinstance(data, dict):
                return data
        except Exception:  # pylint: disable=broad-exception-caught
            pass
    return {
        key: getattr(event, key, None)
        for key in (
            "object",
            "type",
            "status",
            "role",
            "id",
            "msg_id",
            "text",
            "delta",
            "content",
            "output",
        )
        if getattr(event, key, None) is not None
    }


def _is_visible_assistant_message(payload: dict[str, Any]) -> bool:
    return (
        _token(payload.get("object")) == "message"
        and _token(payload.get("role")) == "assistant"
        and _token(payload.get("type")) in _VISIBLE_MESSAGE_TYPES
        and payload.get("error") is None
    )


def _is_reasoning_message(payload: dict[str, Any]) -> bool:
    role = _token(payload.get("role"))
    return (
        _token(payload.get("object")) == "message"
        and _token(payload.get("type")) in _REASONING_MESSAGE_TYPES
        and role in {"", "assistant"}
        and payload.get("error") is None
    )


def _thinking_content_text(value: Any) -> tuple[str, bool]:
    """Return reasoning/thinking text and whether it is incremental."""
    if isinstance(value, str):
        return value, False
    if isinstance(value, (list, tuple)):
        texts: list[str] = []
        has_incremental = False
        for item in value:
            text, incremental = _thinking_content_text(item)
            if text:
                texts.append(text)
                has_incremental = has_incremental or incremental
        return "".join(texts), has_incremental

    payload = _event_mapping(value)
    if not payload or payload.get("error") is not None:
        return "", False

    for key in ("thinking", "reasoning", "text"):
        text = payload.get(key)
        if isinstance(text, str):
            return text, payload.get("delta") is True
    delta = payload.get("delta")
    if isinstance(delta, str):
        return delta, True
    if "content" in payload:
        return _thinking_content_text(payload.get("content"))
    return "", False


def _content_text(  # pylint: disable=too-many-return-statements
    value: Any,
    *,
    nested_visible_content: bool = False,
) -> tuple[str, bool]:
    """Return visible text and whether it is an explicit incremental delta."""
    if isinstance(value, str):
        return (value, False) if nested_visible_content else ("", False)
    if isinstance(value, (list, tuple)):
        texts: list[str] = []
        has_incremental = False
        for item in value:
            text, incremental = _content_text(
                item,
                nested_visible_content=nested_visible_content,
            )
            if text:
                texts.append(text)
                has_incremental = has_incremental or incremental
        return "".join(texts), has_incremental

    payload = _event_mapping(value)
    if not payload or payload.get("error") is not None:
        return "", False

    object_name = _token(payload.get("object"))
    event_type = _token(payload.get("type"))
    if object_name == "response":
        return _content_text(payload.get("output"))
    if object_name == "message":
        if not _is_visible_assistant_message(payload):
            return "", False
        return _content_text(
            payload.get("content"),
            nested_visible_content=True,
        )
    visible_content = object_name == "content" or (
        nested_visible_content and not object_name
    )
    if not visible_content or event_type not in _VISIBLE_TEXT_TYPES:
        return "", False
    text = payload.get("text")
    if isinstance(text, str):
        return text, payload.get("delta") is True
    delta = payload.get("delta")
    if isinstance(delta, str):
        return delta, True
    return "", False


def should_project_runtime_events(
    channel_id: Any,
    channel_meta: Any,
) -> bool:
    """Require both the Runtime channel and a trusted non-empty task id."""
    if str(channel_id or "").strip() != "bank-runtime":
        return False
    if not isinstance(channel_meta, dict):
        return False
    runtime_task_id = channel_meta.get("runtime_task_id")
    return isinstance(runtime_task_id, str) and bool(runtime_task_id.strip())


def _terminal_outcome(payload: dict[str, Any]) -> tuple[bool | None, bool]:
    object_name = _token(payload.get("object"))
    event_name = _token(payload.get("event") or payload.get("event_type"))
    status = _token(payload.get("status"))
    terminal_native = object_name == "response" or bool(event_name)
    if not terminal_native:
        return None, False
    if status in _CANCEL_STATES or event_name in {"cancelled", "canceled"}:
        return False, True
    if status in _FAILURE_STATES or event_name in {
        "failed",
        "error",
        "agentfailed",
        "answerfailed",
    }:
        return False, False
    if status in _SUCCESS_STATES or event_name in {
        "completed",
        "done",
        "success",
        "agentcompleted",
        "answercompleted",
    }:
        return True, False
    return None, False


class RuntimeEventProjector:  # pylint: disable=too-many-instance-attributes
    """Stateful native-event to public Runtime-event projector."""

    def __init__(
        self,
        *,
        min_chunk_chars: int = DEFAULT_MIN_CHUNK_CHARS,
        max_chunk_chars: int = DEFAULT_MAX_CHUNK_CHARS,
        max_chunk_delay_seconds: float = DEFAULT_MAX_CHUNK_DELAY_SECONDS,
    ) -> None:
        if min_chunk_chars <= 0 or max_chunk_chars < min_chunk_chars:
            raise ValueError("Runtime chunk bounds are invalid.")
        if max_chunk_delay_seconds <= 0:
            raise ValueError("Runtime chunk delay must be positive.")
        self.min_chunk_chars = min_chunk_chars
        self.max_chunk_chars = max_chunk_chars
        self.max_chunk_delay_seconds = max_chunk_delay_seconds
        self.accepted_sent = False
        self.full_text = ""
        self.buffer = ""
        self.last_flush_at: float | None = None
        self.terminal_sent = False
        self.internal_parent_message_ids: set[str] = set()
        self.reasoning_parent_message_ids: set[str] = set()
        self.thinking_snapshot_text: dict[str, str] = {}

    def project(
        self,
        native_event: Any,
        *,
        now: float | None = None,
    ) -> list[dict[str, str]]:
        """Consume one native event and return zero or more public events."""
        if self.terminal_sent:
            return []
        current_time = time.monotonic() if now is None else float(now)
        payload = _event_mapping(native_event)
        self._remember_internal_parent(payload)
        terminal_success, cancelled = _terminal_outcome(payload)
        if terminal_success is False:
            return self.finish(
                success=False,
                cancelled=cancelled,
                now=current_time,
            )

        emitted: list[dict[str, str]] = []
        thinking_delta = self._thinking_delta(payload)
        if thinking_delta:
            emitted.append(
                {"event": "answer.thinking", "text": thinking_delta},
            )

        parent_message_id = str(payload.get("msg_id") or "").strip()
        if (
            parent_message_id in self.internal_parent_message_ids
            or parent_message_id in self.reasoning_parent_message_ids
        ):
            text, incremental = "", False
        else:
            text, incremental = _content_text(payload)
        if text:
            delta = self._append_text(text, incremental=incremental)
            if delta:
                if not self.accepted_sent:
                    self.accepted_sent = True
                    emitted.append(
                        {
                            "event": "status.changed",
                            "status": STATUS_ANSWER_GENERATING,
                            "message": "正在生成回答",
                        },
                    )
                emitted.extend(
                    self._flush(
                        current_time,
                        force=False,
                        allow_min_chunk=not incremental,
                    ),
                )
        elif self.buffer:
            emitted.extend(self._flush(current_time, force=False))

        if terminal_success is True:
            emitted.extend(self.finish(success=True, now=current_time))
        return emitted

    def finish(
        self,
        *,
        success: bool,
        now: float | None = None,
        cancelled: bool = False,
        message: str | None = None,
    ) -> list[dict[str, str]]:
        """Flush buffered text and emit one successful or failed terminal."""
        if self.terminal_sent:
            return []
        current_time = time.monotonic() if now is None else float(now)
        emitted = self._flush(current_time, force=True)
        self.terminal_sent = True
        if success:
            emitted.append(
                {
                    "event": "answer.completed",
                    "status": STATUS_ANSWER_COMPLETED,
                    "message": "答案已生成",
                },
            )
            return emitted

        failure_message = "任务已取消" if cancelled else "回答生成失败"
        if message in _SAFE_FAILURE_MESSAGES:
            failure_message = message
        emitted.append(
            {
                "event": "answer.failed",
                "status": STATUS_ANSWER_FAILED,
                "message": failure_message,
            },
        )
        return emitted

    def flush_due(self, *, now: float | None = None) -> list[dict[str, str]]:
        """Flush a buffered chunk when its wall-clock deadline has elapsed."""
        if self.terminal_sent:
            return []
        current_time = time.monotonic() if now is None else float(now)
        return self._flush(current_time, force=False)

    def next_flush_delay_seconds(
        self,
        *,
        now: float | None = None,
    ) -> float | None:
        """Return one pending flush delay, or None when no text is buffered."""
        if self.terminal_sent or not self.buffer:
            return None
        current_time = time.monotonic() if now is None else float(now)
        started_at = (
            self.last_flush_at
            if self.last_flush_at is not None
            else current_time
        )
        return max(
            0.0,
            started_at + self.max_chunk_delay_seconds - current_time,
        )

    def _remember_internal_parent(self, payload: dict[str, Any]) -> None:
        if _token(payload.get("object")) != "message":
            return
        message_id = str(payload.get("id") or "").strip()
        if _is_reasoning_message(payload):
            if message_id:
                self.reasoning_parent_message_ids.add(message_id)
            return
        if message_id and not _is_visible_assistant_message(payload):
            self.internal_parent_message_ids.add(message_id)

    def _thinking_delta(self, payload: dict[str, Any]) -> str:
        if _is_reasoning_message(payload):
            message_id = str(payload.get("id") or "").strip()
            text, incremental = _thinking_content_text(payload.get("content"))
            key = f"message:{message_id}" if message_id else "message"
            return self._new_thinking_delta(key, text, incremental=incremental)

        if _token(payload.get("object")) != "content":
            return ""
        parent_message_id = str(payload.get("msg_id") or "").strip()
        if (
            parent_message_id not in self.reasoning_parent_message_ids
            and _token(payload.get("type")) not in _REASONING_MESSAGE_TYPES
        ):
            return ""
        text, incremental = _thinking_content_text(payload)
        key = f"content:{parent_message_id or payload.get('id') or ''}"
        return self._new_thinking_delta(key, text, incremental=incremental)

    def _new_thinking_delta(
        self,
        key: str,
        text: str,
        *,
        incremental: bool,
    ) -> str:
        if not text:
            return ""
        if incremental:
            return text
        previous = self.thinking_snapshot_text.get(key, "")
        if text == previous or previous.startswith(text):
            return ""
        if text.startswith(previous):
            delta = text[len(previous):]
        elif previous and previous in text:
            delta = text.split(previous, 1)[1]
        else:
            delta = text
        self.thinking_snapshot_text[key] = text
        return delta

    def _append_text(self, text: str, *, incremental: bool) -> str:
        if incremental:
            delta = text
        elif text == self.full_text or self.full_text.startswith(text):
            return ""
        elif text.startswith(self.full_text):
            delta = text[len(self.full_text):]
        elif self.full_text and self.full_text in text:
            delta = text.split(self.full_text, 1)[1]
        elif self.full_text:
            return ""
        else:
            delta = text
        if not delta:
            return ""
        self.full_text += delta
        self.buffer += delta
        return delta

    def _flush(
        self,
        now: float,
        *,
        force: bool,
        allow_min_chunk: bool = False,
    ) -> list[dict[str, str]]:
        if not self.buffer:
            return []
        if self.last_flush_at is None:
            self.last_flush_at = now

        emitted: list[dict[str, str]] = []
        while len(self.buffer) >= self.max_chunk_chars:
            emitted.append(self._pop_chunk(self.max_chunk_chars))
            self.last_flush_at = now

        deadline_reached = (
            now - self.last_flush_at >= self.max_chunk_delay_seconds
        )
        min_chunk_reached = (
            allow_min_chunk and len(self.buffer) >= self.min_chunk_chars
        )
        if self.buffer and (force or deadline_reached or min_chunk_reached):
            emitted.append(
                self._pop_chunk(
                    min(len(self.buffer), self.max_chunk_chars),
                ),
            )
            self.last_flush_at = now
        return emitted

    def _pop_chunk(self, length: int) -> dict[str, str]:
        text = self.buffer[:length]
        self.buffer = self.buffer[length:]
        return {"event": "answer.chunk", "text": text}
