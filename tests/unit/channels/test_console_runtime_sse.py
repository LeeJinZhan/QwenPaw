# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from agentscope.message import Msg
from agentscope_runtime.engine.schemas.agent_schemas import RunStatus

from qwenpaw.agents.react_agent import QwenPawAgent
from qwenpaw.agents import react_agent as react_agent_module
from qwenpaw.agents.tools.runtime_sandbox_oss import (
    RuntimeAttachmentPreparationError,
)
from qwenpaw.app.channels.console.channel import ConsoleChannel
from qwenpaw.app.routers.console import _stream_runtime_console_events


class _FakeEvent:
    def __init__(self, payload: dict) -> None:
        self._payload = dict(payload)
        self.object = payload.get("object")
        self.status = payload.get("status")
        self.type = payload.get("type")
        self.output = payload.get("output")
        self.usage = payload.get("usage")

    def model_dump_json(self) -> str:
        return json.dumps(self._payload, ensure_ascii=False)


def _fake_reply_agent(request_context: dict) -> QwenPawAgent:
    agent = QwenPawAgent.__new__(QwenPawAgent)
    agent._workspace_dir = None
    agent._request_context = request_context
    agent._agent_config = SimpleNamespace(
        running=SimpleNamespace(
            light_context_config=SimpleNamespace(
                tool_result_pruning_config=SimpleNamespace(
                    pruning_recent_msg_max_bytes=4096,
                ),
            ),
            shell_command_timeout=30,
            shell_command_executable="",
        ),
    )
    return agent


@pytest.mark.asyncio
async def test_console_stream_adds_runtime_completed_event_when_process_finishes() -> None:
    channel = ConsoleChannel(
        process=lambda _request: None,
        enabled=True,
        bot_prefix="",
    )

    async def fake_process(_request):
        yield _FakeEvent(
            {
                "object": "response",
                "status": RunStatus.InProgress,
                "type": "response",
                "output": None,
            }
        )
        yield _FakeEvent(
            {
                "object": "message",
                "status": RunStatus.InProgress,
                "type": "plugin_call",
                "content": None,
            }
        )

    channel._process = fake_process  # type: ignore[method-assign]
    request = SimpleNamespace(
        channel="bank-runtime",
        session_id="runtime-session-001",
        user_id="u001",
        channel_meta={"runtime_task_id": "task-runtime-001"},
        input=[],
    )

    events = [event async for event in channel.stream_one(request)]
    payloads = [
        json.loads(event.removeprefix("data:").strip())
        for event in events
        if event.startswith("data:")
    ]

    assert payloads[-1]["event"] == "completed"
    assert payloads[-1]["chat_id"] == "runtime-session-001"


@pytest.mark.asyncio
async def test_runtime_attachment_preparation_failure_streams_one_failed_terminal(
    monkeypatch,
) -> None:
    media_processing_called = False
    channel = ConsoleChannel(
        process=lambda _request: None,
        enabled=True,
        bot_prefix="",
    )
    agent = _fake_reply_agent(
        {
            "attachments_manifest": [
                {"file_id": "file_denied", "source": "current_task"},
            ],
            "sandbox_context": {
                "task_id": "task-runtime-failed",
                "context_id": "ctx-runtime-failed",
            },
        },
    )

    async def fail_preload(_msg, _request_context):
        raise RuntimeAttachmentPreparationError(
            "file_denied",
            "FILE_ACCESS_DENIED",
        )

    async def observe_media_processing(_msg):
        nonlocal media_processing_called
        media_processing_called = True

    monkeypatch.setattr(
        react_agent_module,
        "_append_runtime_attachment_content_parts",
        fail_preload,
    )
    monkeypatch.setattr(
        react_agent_module,
        "process_file_and_media_blocks_in_message",
        observe_media_processing,
    )

    async def failing_process(_request):
        await QwenPawAgent.reply.__wrapped__(
            agent,
            Msg("user", "识别附件", "user"),
        )
        yield  # pragma: no cover

    channel._process = failing_process  # type: ignore[method-assign]
    native_payload = {
        "channel_id": "bank-runtime",
        "sender_id": "u001",
        "content_parts": [{"type": "text", "text": "识别附件"}],
        "meta": {
            "session_id": "runtime-session-failed",
            "runtime_task_id": "task-runtime-failed",
            "sandbox_context": {"task_id": "task-runtime-failed"},
        },
    }

    events = [
        event
        async for event in _stream_runtime_console_events(
            channel,
            native_payload,
        )
    ]
    payloads = [
        json.loads(event.removeprefix("data:").strip())
        for event in events
        if event.startswith("data:")
    ]

    assert media_processing_called is False
    assert len(payloads) == 1
    assert payloads[0]["event"] == "failed"
    assert payloads[0]["status"] == "failed"
    assert payloads[0]["reason_code"] == "FILE_ACCESS_DENIED"
    assert payloads[0]["chat_id"] == "runtime-session-failed"
    assert all(payload.get("event") != "completed" for payload in payloads)


@pytest.mark.asyncio
async def test_invalid_runtime_attachment_file_id_streams_failed_not_completed(
    monkeypatch,
) -> None:
    media_processing_called = False
    channel = ConsoleChannel(
        process=lambda _request: None,
        enabled=True,
        bot_prefix="",
    )
    agent = _fake_reply_agent(
        {
            "attachments_manifest": [
                {"file_id": "../invalid", "source": "current_task"},
            ],
            "sandbox_context": {
                "task_id": "task-runtime-invalid",
                "context_id": "ctx-runtime-invalid",
            },
        },
    )

    async def observe_media_processing(_msg):
        nonlocal media_processing_called
        media_processing_called = True

    monkeypatch.setattr(
        react_agent_module,
        "process_file_and_media_blocks_in_message",
        observe_media_processing,
    )

    async def failing_process(_request):
        await QwenPawAgent.reply.__wrapped__(
            agent,
            Msg("user", "识别附件", "user"),
        )
        yield  # pragma: no cover

    channel._process = failing_process  # type: ignore[method-assign]
    native_payload = {
        "channel_id": "bank-runtime",
        "sender_id": "u001",
        "content_parts": [{"type": "text", "text": "识别附件"}],
        "meta": {
            "session_id": "runtime-session-invalid",
            "runtime_task_id": "task-runtime-invalid",
            "sandbox_context": {"task_id": "task-runtime-invalid"},
        },
    }

    events = [
        event
        async for event in _stream_runtime_console_events(
            channel,
            native_payload,
        )
    ]
    payloads = [
        json.loads(event.removeprefix("data:").strip())
        for event in events
        if event.startswith("data:")
    ]

    assert media_processing_called is False
    assert len(payloads) == 1
    assert payloads[0]["event"] == "failed"
    assert payloads[0]["status"] == "failed"
    assert payloads[0]["reason_code"] == "ATTACHMENT_INPUT_INVALID"
    assert all(payload.get("event") != "completed" for payload in payloads)


@pytest.mark.asyncio
async def test_runtime_attachment_filesystem_error_streams_failed_not_completed(
    monkeypatch,
) -> None:
    media_processing_called = False
    cleaned: list[str] = []
    channel = ConsoleChannel(
        process=lambda _request: None,
        enabled=True,
        bot_prefix="",
    )
    agent = _fake_reply_agent(
        {
            "attachments_manifest": [
                {"file_id": "file_broken", "source": "current_task"},
            ],
            "sandbox_context": {
                "task_id": "task-runtime-broken",
                "context_id": "ctx-runtime-broken",
            },
        },
    )

    class FilesystemFailingCache:
        class Reservation:
            def release(self) -> None:
                pass

        def reserve_task_io(self, *_args, **_kwargs):
            return self.Reservation()

        def prepare_files(self, *_args, **_kwargs):
            raise OSError("temporary attachment directory is unavailable")

        def cleanup_task(self, task_id: str) -> None:
            cleaned.append(task_id)

    async def observe_media_processing(_msg):
        nonlocal media_processing_called
        media_processing_called = True

    monkeypatch.setattr(
        react_agent_module.runtime_sandbox_oss,
        "_DEFAULT_TASK_ATTACHMENT_CACHE",
        FilesystemFailingCache(),
    )
    monkeypatch.setattr(
        react_agent_module,
        "process_file_and_media_blocks_in_message",
        observe_media_processing,
    )

    async def failing_process(_request):
        await QwenPawAgent.reply.__wrapped__(
            agent,
            Msg("user", "识别附件", "user"),
        )
        yield  # pragma: no cover

    channel._process = failing_process  # type: ignore[method-assign]
    native_payload = {
        "channel_id": "bank-runtime",
        "sender_id": "u001",
        "content_parts": [{"type": "text", "text": "识别附件"}],
        "meta": {
            "session_id": "runtime-session-broken",
            "runtime_task_id": "task-runtime-broken",
            "sandbox_context": {"task_id": "task-runtime-broken"},
        },
    }

    events = [
        event
        async for event in _stream_runtime_console_events(
            channel,
            native_payload,
        )
    ]
    payloads = [
        json.loads(event.removeprefix("data:").strip())
        for event in events
        if event.startswith("data:")
    ]

    assert media_processing_called is False
    assert cleaned == ["task-runtime-broken"]
    assert len(payloads) == 1
    assert payloads[0]["event"] == "failed"
    assert payloads[0]["reason_code"] == "ATTACHMENT_READ_FAILED"
    assert all(payload.get("event") != "completed" for payload in payloads)
