# -*- coding: utf-8 -*-
"""Unit tests for ``console._extract_placeholder_name``.

The console handler picks an immediate placeholder name for a new chat
from the first content part. Shapes match the agentscope content-block
formats (``{"type": "text", "text": "..."}`` dicts, ``TextBlock``-like
objects with ``.text``, raw strings, and non-text/media blocks). These
tests pin that mapping so a future shape change cannot silently produce
labels like ``{"type": ...`` in the session drawer (regression for PR #3).
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from agentscope_runtime.engine.schemas.agent_schemas import AgentRequest

from qwenpaw.app.channels.console.runtime_event_projection import (
    RuntimeEventProjector,
)
from qwenpaw.app.routers.console import (
    _extract_placeholder_name,
    _extract_session_and_payload,
    _is_runtime_native_payload,
    _is_runtime_terminal_sse,
    _runtime_missing_terminal_sse,
    _stream_runtime_console_events,
)


class _TextBlock:
    """Stand-in for an agentscope ``TextBlock`` (object with ``.text``)."""

    def __init__(self, text: str) -> None:
        self.text = text


class _RuntimeConsoleChannel:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.seen_payload = None

    async def stream_one(self, payload):
        self.seen_payload = payload
        for event in self.events:
            yield event


class _RaisingRuntimeConsoleChannel:
    def __init__(self, error: BaseException) -> None:
        self.error = error

    async def stream_one(self, _payload):
        if False:
            yield ""
        raise self.error


class _CancellableRuntimeConsoleChannel:
    async def stream_one(self, _payload):
        yield 'data: {"event": "message"}\n\n'
        await asyncio.Event().wait()


def test_no_content_parts_returns_new_chat() -> None:
    name, first_text = _extract_placeholder_name([])
    assert name == "New Chat"
    assert first_text == ""


def test_string_content_part() -> None:
    name, first_text = _extract_placeholder_name(["Hello, world!"])
    assert name == "Hello, wor"
    assert first_text == "Hello, world!"


def test_dict_text_block() -> None:
    """``{"type": "text", "text": "..."}`` is the agentscope text block.

    Without the dict-aware branch this would fall through to
    ``str(content)`` and produce a placeholder like ``{'type': ...``.
    """
    parts = [{"type": "text", "text": "What's the weather today?"}]
    name, first_text = _extract_placeholder_name(parts)
    assert name == "What's the"
    assert first_text == "What's the weather today?"


def test_dict_without_text_key_is_treated_as_media() -> None:
    """Image/audio dict blocks lack a ``text`` field and should not produce
    JSON-shaped placeholders."""
    parts = [{"type": "image", "image": {"url": "x.png"}}]
    name, first_text = _extract_placeholder_name(parts)
    assert name == "Media Message"
    assert first_text == ""


def test_dict_with_non_string_text_is_treated_as_media() -> None:
    parts = [{"type": "text", "text": 123}]
    name, first_text = _extract_placeholder_name(parts)
    assert name == "Media Message"
    assert first_text == ""


def test_object_with_text_attribute() -> None:
    parts = [_TextBlock("Plan a trip to Tokyo next week")]
    name, first_text = _extract_placeholder_name(parts)
    assert name == "Plan a tri"
    assert first_text == "Plan a trip to Tokyo next week"


def test_object_with_empty_text_attribute_is_media() -> None:
    parts = [_TextBlock("")]
    name, first_text = _extract_placeholder_name(parts)
    assert name == "Media Message"
    assert first_text == ""


def test_unknown_shape_is_treated_as_media() -> None:
    """Unknown blocks must NOT be ``str(...)``-coerced into a placeholder."""
    parts = [object()]
    name, first_text = _extract_placeholder_name(parts)
    assert name == "Media Message"
    assert first_text == ""


def test_falsy_first_part_is_media() -> None:
    name, first_text = _extract_placeholder_name([None])
    assert name == "Media Message"
    assert first_text == ""


def test_extract_session_payload_preserves_runtime_bank_context_in_meta() -> None:
    payload = {
        "channel": "bank-runtime",
        "user_id": "u001",
        "session_id": "session-runtime-001",
        "trace_id": "trace-runtime-001",
        "runtime_task_id": "task-runtime-001",
        "runtime_execution_mode": "simple_text_fast",
        "runtime_response_mode": "stream_answer",
        "runtime_datetime_context": {
            "current_date": "2026-07-02",
            "current_datetime": "2026-07-02T11:48:14+08:00",
            "timezone": "Asia/Shanghai",
        },
        "runtime_latency_marks": {
            "submit_received_at": "2026-07-02T11:48:14+08:00",
            "worker_dispatch_started_at": "2026-07-02T11:48:18+08:00",
        },
        "identity_json": '{"user_id":"u001","allowed_customer_ids":["cust-001"]}',
        "runtime_governance": {
            "task_id": "task-runtime-001",
            "trace_id": "trace-runtime-001",
            "user_id": "u001",
        },
        "runtime_tool_gateway": {
            "endpoint": "http://runtime.local/runtime/v1/tool-calls",
            "allowed_tools": ["ragflow.search_policy"],
        },
        "attachments_manifest": [
            {
                "file_id": "file_001",
                "original_name": "客户材料.md",
                "access_mode": "sandbox_oss",
            }
        ],
        "sandbox_context": {
            "context_id": "ctx_001",
            "task_id": "task-runtime-001",
            "user_id": "u001",
            "assistant_id": "general_assistant",
            "scope": {"file_scope": "current_user_current_assistant"},
            "expires_at": "2026-07-02T12:00:00+08:00",
            "signature": "signed",
        },
        "runtime_constraints": {
            "disabled_tools": ["write_file"],
        },
        "runtime_context": {
            "user_overlay": {
                "profile": {
                    "trust_level": "low",
                    "preferences": {"language": "en-US"},
                },
            },
        },
        "personal_skills_catalog": {
            "snapshot_id": "pss_001",
            "items": [],
            "limits": {
                "max_candidate_loads": 3,
                "max_activated": 3,
                "max_activated_bytes": 65536,
            },
        },
        "personal_skills_access_manifest": {
            "snapshot_id": "pss_001",
            "items": [],
        },
        "input": [
            {
                "role": "user",
                "content": [{"type": "text", "text": "查询客户授信政策"}],
            },
        ],
    }

    native_payload = _extract_session_and_payload(payload)

    assert native_payload["channel_id"] == "bank-runtime"
    assert native_payload["sender_id"] == "u001"
    assert native_payload["content_parts"] == [{"type": "text", "text": "查询客户授信政策"}]
    assert native_payload["meta"]["session_id"] == "session-runtime-001"
    assert native_payload["meta"]["user_id"] == "u001"
    assert native_payload["meta"]["trace_id"] == "trace-runtime-001"
    assert native_payload["meta"]["runtime_task_id"] == "task-runtime-001"
    assert native_payload["meta"]["runtime_execution_mode"] == "simple_text_fast"
    assert native_payload["meta"]["runtime_response_mode"] == "stream_answer"
    assert native_payload["meta"]["runtime_datetime_context"] == payload["runtime_datetime_context"]
    assert native_payload["meta"]["runtime_latency_marks"] == payload["runtime_latency_marks"]
    assert native_payload["meta"]["identity_json"] == payload["identity_json"]
    assert native_payload["meta"]["runtime_governance"] == payload["runtime_governance"]
    assert native_payload["meta"]["runtime_tool_gateway"] == payload["runtime_tool_gateway"]
    assert native_payload["meta"]["attachments_manifest"] == payload["attachments_manifest"]
    assert native_payload["meta"]["sandbox_context"] == payload["sandbox_context"]
    assert native_payload["meta"]["runtime_constraints"] == payload["runtime_constraints"]
    assert native_payload["meta"]["runtime_context"] == payload["runtime_context"]
    assert native_payload["meta"]["personal_skills_catalog"] == payload[
        "personal_skills_catalog"
    ]
    assert native_payload["meta"]["personal_skills_access_manifest"] == payload[
        "personal_skills_access_manifest"
    ]


def test_extract_runtime_payload_uses_only_current_user_message() -> None:
    payload = {
        "channel": "bank-runtime",
        "user_id": "u001",
        "session_id": "session-runtime-001",
        "runtime_task_id": "task-runtime-001",
        "input": [
            {
                "role": "user",
                "content": [{"type": "text", "text": "列出助手文件"}],
            },
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "第一份是截图"}],
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "识别这次上传的 Excel"},
                    {
                        "type": "file",
                        "file_id": "file_current_001",
                        "name": "current.xlsx",
                    },
                ],
            },
        ],
    }

    native_payload = _extract_session_and_payload(payload)

    assert native_payload["content_parts"] == payload["input"][-1]["content"]


def test_extract_agent_request_payload_preserves_runtime_bank_context_in_meta() -> None:
    request = AgentRequest(
        channel="bank-runtime",
        user_id="u001",
        session_id="session-runtime-001",
        trace_id="trace-runtime-001",
        runtime_task_id="task-runtime-001",
        identity_json={"user_id": "u001", "allowed_customer_ids": ["cust-001"]},
        runtime_governance={
            "task_id": "task-runtime-001",
            "trace_id": "trace-runtime-001",
            "user_id": "u001",
        },
        runtime_tool_gateway={
            "endpoint": "http://runtime.local/runtime/v1/tool-calls",
            "allowed_tools": ["workspace.list_outputs"],
        },
        runtime_constraints={
            "disabled_tools": ["execute_shell_command", "write_file"],
        },
        input=[
            {
                "role": "user",
                "content": [{"type": "text", "text": "查询客户授信政策"}],
            },
        ],
    )

    native_payload = _extract_session_and_payload(request)

    assert native_payload["channel_id"] == "bank-runtime"
    assert native_payload["sender_id"] == "u001"
    assert native_payload["meta"]["session_id"] == "session-runtime-001"
    assert native_payload["meta"]["user_id"] == "u001"
    assert native_payload["meta"]["trace_id"] == "trace-runtime-001"
    assert native_payload["meta"]["runtime_task_id"] == "task-runtime-001"
    assert native_payload["meta"]["identity_json"] == request.identity_json
    assert native_payload["meta"]["runtime_governance"] == request.runtime_governance
    assert native_payload["meta"]["runtime_tool_gateway"] == request.runtime_tool_gateway
    assert native_payload["meta"]["runtime_constraints"] == request.runtime_constraints


def test_runtime_native_payload_detection_requires_bank_runtime_channel() -> None:
    assert _is_runtime_native_payload({"channel_id": "bank-runtime", "meta": {}})
    assert not _is_runtime_native_payload(
        {"channel_id": "console", "meta": {"runtime_task_id": "task_001"}},
    )
    assert not _is_runtime_native_payload(
        {"channel_id": "console", "meta": {"runtime_tool_gateway": {}}},
    )
    assert not _is_runtime_native_payload(
        {"channel_id": "console", "meta": {"runtime_governance": {}}},
    )
    assert not _is_runtime_native_payload({"channel_id": "console", "meta": {}})


def test_runtime_missing_terminal_sse_is_safe_failure() -> None:
    event = _runtime_missing_terminal_sse(
        {"meta": {"session_id": "runtime-session-001"}},
    )

    assert event == (
        'data: {"event": "answer.failed", "status": "failed", '
        '"message": "回答生成失败"}\n\n'
    )
    assert _is_runtime_terminal_sse(event)
    assert _is_runtime_terminal_sse('data: {"status": "completed", "object": "response"}\n\n')
    assert not _is_runtime_terminal_sse('data: {"status": "in_progress", "object": "response"}\n\n')
    assert not _is_runtime_terminal_sse('data: {"status": "completed", "object": "content", "type": "text"}\n\n')
    assert not _is_runtime_terminal_sse('data: {"status": "completed", "object": "message", "type": "plugin_call_output"}\n\n')


def test_runtime_console_events_fail_when_upstream_has_no_terminal() -> None:
    native_payload = {
        "channel_id": "bank-runtime",
        "sender_id": "u001",
        "meta": {
            "session_id": "runtime-session-001",
            "runtime_task_id": "task-001",
            "sandbox_context": {"task_id": "task-001"},
        },
    }
    channel = _RuntimeConsoleChannel(['data: {"event": "message", "delta": "处理中"}\n\n'])

    events = asyncio.run(_collect_runtime_events(channel, native_payload))

    assert channel.seen_payload == native_payload
    assert events == [
        'data: {"event": "message", "delta": "处理中"}\n\n',
        'data: {"event": "answer.failed", "status": "failed", '
        '"message": "回答生成失败"}\n\n',
    ]


def test_runtime_console_events_fail_after_content_completed_only() -> None:
    native_payload = {
        "channel_id": "bank-runtime",
        "sender_id": "u001",
        "meta": {
            "session_id": "runtime-session-001",
            "runtime_task_id": "task-001",
            "sandbox_context": {"task_id": "task-001"},
        },
    }
    content_completed = 'data: {"object": "content", "status": "completed", "type": "text"}\n\n'
    channel = _RuntimeConsoleChannel([content_completed])

    events = asyncio.run(_collect_runtime_events(channel, native_payload))

    assert events == [
        content_completed,
        'data: {"event": "answer.failed", "status": "failed", '
        '"message": "回答生成失败"}\n\n',
    ]


def test_runtime_console_events_do_not_duplicate_terminal() -> None:
    native_payload = {
        "channel_id": "bank-runtime",
        "sender_id": "u001",
        "meta": {
            "session_id": "runtime-session-001",
            "runtime_task_id": "task-001",
            "sandbox_context": {"task_id": "task-001"},
        },
    }
    terminal = 'data: {"status": "completed", "object": "response"}\n\n'
    channel = _RuntimeConsoleChannel([terminal])

    events = asyncio.run(_collect_runtime_events(channel, native_payload))

    assert events == [terminal]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "channel",
    [
        _RuntimeConsoleChannel(
            ['data: {"status": "completed", "object": "response"}\n\n'],
        ),
        _RaisingRuntimeConsoleChannel(RuntimeError("failed")),
        _RaisingRuntimeConsoleChannel(asyncio.TimeoutError()),
    ],
    ids=["success", "failure", "timeout"],
)
async def test_runtime_console_events_cleanup_task_on_terminal_paths(
    monkeypatch,
    channel,
) -> None:
    sandbox_module = __import__(
        "qwenpaw.agents.tools.runtime_sandbox_oss",
        fromlist=["_DEFAULT_TASK_ATTACHMENT_CACHE"],
    )
    cleaned: list[str] = []
    monkeypatch.setattr(
        sandbox_module._DEFAULT_TASK_ATTACHMENT_CACHE,
        "cleanup_task",
        lambda task_id: cleaned.append(task_id),
    )
    payload = {
        "channel_id": "bank-runtime",
        "meta": {
            "runtime_task_id": "task-cleanup",
            "sandbox_context": {"task_id": "task-cleanup"},
        },
    }

    if isinstance(channel, _RuntimeConsoleChannel):
        await _collect_runtime_events(channel, payload)
    else:
        with pytest.raises(type(channel.error)):
            await _collect_runtime_events(channel, payload)

    assert cleaned == ["task-cleanup"]


@pytest.mark.asyncio
async def test_runtime_console_events_cleanup_task_on_cancellation(monkeypatch) -> None:
    sandbox_module = __import__(
        "qwenpaw.agents.tools.runtime_sandbox_oss",
        fromlist=["_DEFAULT_TASK_ATTACHMENT_CACHE"],
    )
    cleaned: list[str] = []
    monkeypatch.setattr(
        sandbox_module._DEFAULT_TASK_ATTACHMENT_CACHE,
        "cleanup_task",
        lambda task_id: cleaned.append(task_id),
    )
    payload = {
        "channel_id": "bank-runtime",
        "meta": {
            "runtime_task_id": "task-cancel",
            "sandbox_context": {"task_id": "task-cancel"},
        },
    }
    stream = _stream_runtime_console_events(
        _CancellableRuntimeConsoleChannel(),
        payload,
    )
    assert await anext(stream) == 'data: {"event": "message"}\n\n'
    pending = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)

    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    await stream.aclose()

    assert cleaned == ["task-cancel"]


@pytest.mark.asyncio
async def test_runtime_cancel_waits_for_queued_prepare_before_cleanup(
    monkeypatch,
    tmp_path,
) -> None:
    sandbox_module = __import__(
        "qwenpaw.agents.tools.runtime_sandbox_oss",
        fromlist=["_DEFAULT_TASK_ATTACHMENT_CACHE"],
    )
    assert hasattr(sandbox_module, "run_task_io_in_thread")
    cache = sandbox_module.TaskAttachmentCache(root=tmp_path)
    submitted = asyncio.Event()
    release_worker = asyncio.Event()

    class StreamingClient:
        def authorize_file(self, file_id, _sandbox_context):
            return {
                "file_id": file_id,
                "storage_provider": "local",
                "object_key": f"runtime/{file_id}",
                "content_type": "text/plain",
                "size_bytes": 6,
                "original_name": "queued.txt",
            }

        def stream_authorized_locator(self, _locator, write_chunk):
            write_chunk(b"queued")
            return "text/plain"

    async def delayed_thread_submission(function, *args, **kwargs):
        submitted.set()
        await release_worker.wait()
        return function(*args, **kwargs)

    class QueuedPrepareChannel:
        async def stream_one(self, _payload):
            await sandbox_module.run_task_io_in_thread(
                cache,
                "task-queued",
                cache.prepare_file,
                "file-queued",
                {"task_id": "task-queued", "context_id": "ctx-queued"},
                client=StreamingClient(),
                file_id="file-queued",
            )
            yield 'data: {"status": "completed", "object": "response"}\n\n'

    monkeypatch.setattr(
        sandbox_module,
        "_DEFAULT_TASK_ATTACHMENT_CACHE",
        cache,
    )
    monkeypatch.setattr(
        sandbox_module,
        "_run_in_thread",
        delayed_thread_submission,
        raising=False,
    )
    stream = _stream_runtime_console_events(
        QueuedPrepareChannel(),
        {
            "channel_id": "bank-runtime",
            "meta": {
                "runtime_task_id": "task-queued",
                "sandbox_context": {"task_id": "task-queued"},
            },
        },
    )
    pending = asyncio.create_task(anext(stream))
    await asyncio.wait_for(submitted.wait(), timeout=1)
    assert cache._io_reservations == {"task-queued": 2}

    pending.cancel()
    await asyncio.sleep(0.05)
    assert pending.done() is False
    release_worker.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(pending, timeout=2)
    await stream.aclose()

    assert not list(tmp_path.rglob("queued.txt"))
    assert cache._prepared == {}
    assert cache._io_reservations == {}
    assert cache._download_locks == {}
    assert cache._batch_locks == {}
    assert cache._batch_lock_users == {}
    assert cache._cleaning_tasks == set()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("meta", "expected_cleaned"),
    [
        ({"sandbox_context": {"task_id": "task-actual"}}, {"task-actual"}),
        ({"runtime_task_id": "task-declared"}, {"task-declared"}),
        (
            {
                "runtime_task_id": "task-declared",
                "sandbox_context": {"task_id": "task-actual"},
            },
            {"task-actual", "task-declared"},
        ),
    ],
    ids=["missing-runtime-task", "missing-sandbox-task", "mismatched-task"],
)
async def test_runtime_console_rejects_invalid_task_binding_before_channel(
    monkeypatch,
    meta,
    expected_cleaned,
) -> None:
    sandbox_module = __import__(
        "qwenpaw.agents.tools.runtime_sandbox_oss",
        fromlist=["_DEFAULT_TASK_ATTACHMENT_CACHE"],
    )
    cleaned: list[str] = []
    monkeypatch.setattr(
        sandbox_module._DEFAULT_TASK_ATTACHMENT_CACHE,
        "cleanup_task",
        lambda task_id: cleaned.append(task_id),
    )

    class UnexpectedChannel:
        def __init__(self) -> None:
            self.called = False

        async def stream_one(self, _payload):
            self.called = True
            yield 'data: {"status": "completed", "object": "response"}\n\n'

    channel = UnexpectedChannel()
    events = await _collect_runtime_events(
        channel,
        {"channel_id": "bank-runtime", "meta": meta},
    )

    assert channel.called is False
    assert set(cleaned) == expected_cleaned
    assert len(events) == 1
    payload = json.loads(events[0].removeprefix("data: ").strip())
    assert payload == {
        "event": "answer.failed",
        "status": "failed",
        "message": "回答生成失败",
    }


@pytest.mark.asyncio
async def test_runtime_console_holds_task_reservation_for_full_model_stream(
    monkeypatch,
    tmp_path,
) -> None:
    sandbox_module = __import__(
        "qwenpaw.agents.tools.runtime_sandbox_oss",
        fromlist=["_DEFAULT_TASK_ATTACHMENT_CACHE"],
    )
    cache = sandbox_module.TaskAttachmentCache(root=tmp_path, ttl_seconds=1)
    now = time.time()
    marker = sandbox_module.TaskMarker(
        task_id="task-active",
        sandbox_context_id="ctx-active",
        created_at_epoch=now - 10,
        expires_at_epoch=now - 1,
    )
    cache._ensure_private_task_root("task-active")
    cache._write_task_marker(marker)
    cache._markers["task-active"] = marker
    task_root = cache._task_root("task-active")
    observed_reservations: list[int] = []
    active_root_seen: list[bool] = []
    cleanup_reservations: list[int] = []
    original_cleanup = cache.cleanup_task

    def observe_cleanup(task_id: str) -> None:
        cleanup_reservations.append(cache._io_reservations.get(task_id, 0))
        original_cleanup(task_id)

    cache.cleanup_task = observe_cleanup
    monkeypatch.setattr(
        sandbox_module,
        "_DEFAULT_TASK_ATTACHMENT_CACHE",
        cache,
    )

    class ActiveModelChannel:
        async def stream_one(self, _payload):
            observed_reservations.append(
                cache._io_reservations.get("task-active", 0),
            )
            cache.sweep_expired(now_epoch=now + 1)
            active_root_seen.append(task_root.is_dir())
            yield 'data: {"status": "completed", "object": "response"}\n\n'

    events = await _collect_runtime_events(
        ActiveModelChannel(),
        {
            "channel_id": "bank-runtime",
            "meta": {
                "runtime_task_id": "task-active",
                "sandbox_context": {"task_id": "task-active"},
            },
        },
    )

    assert len(events) == 1
    assert observed_reservations == [1]
    assert active_root_seen == [True]
    assert cleanup_reservations == [0]
    assert task_root.exists() is False
    assert cache._io_reservations == {}


@pytest.mark.asyncio
async def test_non_runtime_console_events_do_not_cleanup_task(monkeypatch) -> None:
    sandbox_module = __import__(
        "qwenpaw.agents.tools.runtime_sandbox_oss",
        fromlist=["_DEFAULT_TASK_ATTACHMENT_CACHE"],
    )
    cleaned: list[str] = []
    monkeypatch.setattr(
        sandbox_module._DEFAULT_TASK_ATTACHMENT_CACHE,
        "cleanup_task",
        lambda task_id: cleaned.append(task_id),
    )

    channel = _RuntimeConsoleChannel([])
    await _collect_runtime_events(
        channel,
        {
            "channel_id": "console",
            "meta": {"runtime_task_id": "task-console-metadata"},
        },
    )

    assert channel.seen_payload is not None
    assert cleaned == []


async def _collect_runtime_events(channel, native_payload: dict) -> list[str]:
    return [event async for event in _stream_runtime_console_events(channel, native_payload)]


def test_runtime_event_projector_coalesces_small_text_deltas_before_flush() -> None:
    projector = RuntimeEventProjector(
        min_chunk_chars=256,
        max_chunk_chars=512,
        max_chunk_delay_seconds=0.5,
    )
    emitted: list[dict[str, str]] = []
    delta_text = "短句" * 10

    for index in range(20):
        emitted.extend(
            projector.project(
                {
                    "object": "content",
                    "type": "text",
                    "status": "in_progress",
                    "delta": True,
                    "text": delta_text,
                },
                now=1.0 + index * 0.01,
            ),
        )

    chunks_before_terminal = [
        event for event in emitted if event["event"] == "answer.chunk"
    ]
    emitted.extend(projector.finish(success=True, now=1.4))
    chunks = [event for event in emitted if event["event"] == "answer.chunk"]

    assert chunks_before_terminal == []
    assert len(chunks) == 1
    assert chunks[0]["text"] == delta_text * 20


def test_runtime_event_projector_flushes_last_chunk_before_completed() -> None:
    projector = RuntimeEventProjector(
        min_chunk_chars=256,
        max_chunk_chars=512,
        max_chunk_delay_seconds=0.5,
    )

    emitted = projector.project(
        {
            "object": "content",
            "type": "text",
            "status": "in_progress",
            "delta": True,
            "text": "最后一段",
        },
        now=2.0,
    )
    emitted.extend(projector.project({"event": "completed"}, now=2.1))

    assert [event["event"] for event in emitted[-2:]] == [
        "answer.chunk",
        "answer.completed",
    ]
    assert emitted[-2]["text"] == "最后一段"


def test_runtime_event_projector_emits_only_user_facing_statuses() -> None:
    allowed_statuses = {
        "task.accepted",
        "file.reading",
        "answer.preparing",
        "answer.generating",
        "completed",
        "failed",
    }
    success_projector = RuntimeEventProjector()
    success_events = success_projector.project(
        {
            "object": "content",
            "type": "text",
            "status": "in_progress",
            "delta": True,
            "text": "生成中",
        },
        now=3.0,
    )
    success_events.extend(
        success_projector.project({"event": "completed"}, now=3.1),
    )

    failure_projector = RuntimeEventProjector()
    failure_events = failure_projector.project({"event": "failed"}, now=4.0)
    statuses = [
        event["status"]
        for event in success_events + failure_events
        if "status" in event
    ]

    assert statuses == [
        "answer.generating",
        "completed",
        "failed",
    ]
    assert set(statuses) <= allowed_statuses
