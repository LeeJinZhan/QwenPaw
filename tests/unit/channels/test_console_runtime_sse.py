# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from agentscope.message import Msg
from agentscope_runtime.engine.schemas.agent_schemas import (
    Message as AgentScopeMessage,
    RunStatus,
    TextContent as AgentScopeTextContent,
)

from qwenpaw.agents.react_agent import QwenPawAgent
from qwenpaw.agents import react_agent as react_agent_module
from qwenpaw.agents.tools.runtime_sandbox_oss import (
    RuntimeAttachmentPreparationError,
)
from qwenpaw.app.channels.console.channel import ConsoleChannel
from qwenpaw.app.channels.console.runtime_event_projection import (
    RuntimeEventProjector,
)
from qwenpaw.app.routers.console import _stream_runtime_console_events


def _runtime_projector(**kwargs):
    from qwenpaw.app.channels.console.runtime_event_projection import (
        RuntimeEventProjector,
    )

    return RuntimeEventProjector(**kwargs)


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
async def test_console_stream_adds_runtime_completed_event() -> None:
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

    assert payloads == [
        {
            "event": "answer.completed",
            "status": "completed",
            "message": "答案已生成",
        },
    ]


@pytest.mark.asyncio
async def test_runtime_attachment_failure_streams_one_failed_terminal(
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
    assert payloads[0]["event"] == "answer.failed"
    assert payloads[0]["status"] == "failed"
    assert payloads[0]["message"] == "附件读取失败"
    assert all(
        payload.get("event") != "answer.completed"
        for payload in payloads
    )


@pytest.mark.asyncio
async def test_invalid_attachment_file_id_streams_failed_not_completed(
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
    assert payloads[0]["event"] == "answer.failed"
    assert payloads[0]["status"] == "failed"
    assert payloads[0]["message"] == "附件读取失败"
    assert all(
        payload.get("event") != "answer.completed"
        for payload in payloads
    )


@pytest.mark.asyncio
async def test_attachment_filesystem_error_streams_failed_not_completed(
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
    assert payloads[0]["event"] == "answer.failed"
    assert payloads[0]["status"] == "failed"
    assert payloads[0]["message"] == "附件读取失败"
    assert all(
        payload.get("event") != "answer.completed"
        for payload in payloads
    )


def test_projection_coalesces_event_storm_and_emits_one_terminal() -> None:
    projector = _runtime_projector(
        min_chunk_chars=128,
        max_chunk_delay_seconds=0.075,
    )
    emitted: list[dict] = []

    for index in range(3750):
        event_kind = index % 4
        if event_kind == 0:
            native_event = {
                "object": "message",
                "type": "reasoning",
                "status": "inprogress",
                "content": None,
                "sequence_number": index,
            }
        elif event_kind == 1:
            native_event = {
                "object": "usage",
                "type": "usage",
                "status": "inprogress",
                "usage": {"input_tokens": index},
            }
        elif event_kind == 2:
            native_event = {
                "object": "message",
                "type": "plugin_call_output",
                "status": "completed",
                "content": [
                    {"type": "text", "text": "/tmp/private-tool-result"},
                ],
            }
        else:
            native_event = {
                "object": "content",
                "type": "text",
                "status": "inprogress",
                "text": "今天是 7 月 10 日。",
            }
        emitted.extend(
            projector.project(
                native_event,
                now=0.001 * index,
            ),
        )
    emitted.extend(
        projector.project(
            {
                "object": "content",
                "type": "text",
                "status": "completed",
                "text": "今天是 7 月 10 日。",
            },
            now=4.0,
        ),
    )
    emitted.extend(projector.finish(success=True, now=4.01))

    assert [item["event"] for item in emitted] == [
        "status.changed",
        "answer.chunk",
        "answer.completed",
    ]
    assert emitted[-1] == {
        "event": "answer.completed",
        "status": "completed",
        "message": "答案已生成",
    }


def test_runtime_projection_diffs_cumulative_text_snapshots() -> None:
    projector = _runtime_projector(
        min_chunk_chars=128,
        max_chunk_delay_seconds=0.075,
    )
    emitted: list[dict] = []
    full_text = "甲" * 160

    for length in (40, 80, 128, 128, 160):
        emitted.extend(
            projector.project(
                {
                    "object": "content",
                    "type": "text",
                    "status": "inprogress",
                    "text": full_text[:length],
                },
                now=0.01,
            ),
        )
    emitted.extend(projector.finish(success=True, now=0.02))

    chunks = [
        item["text"]
        for item in emitted
        if item["event"] == "answer.chunk"
    ]
    assert "".join(chunks) == full_text
    assert len(chunks) == 2


def test_runtime_projection_projects_reasoning_message_as_answer_thinking() -> None:
    projector = _runtime_projector()

    emitted = projector.project(
        {
            "object": "message",
            "id": "msg-reasoning",
            "role": "assistant",
            "type": "reasoning",
            "status": "in_progress",
            "content": "我先判断问题类型。",
        },
        now=0.0,
    )

    assert emitted == [
        {"event": "answer.thinking", "text": "我先判断问题类型。"},
    ]


def test_runtime_projection_projects_reasoning_content_deltas_as_answer_thinking() -> None:
    projector = _runtime_projector()

    emitted = projector.project(
        {
            "object": "message",
            "id": "msg-reasoning-delta",
            "role": "assistant",
            "type": "reasoning",
            "status": "in_progress",
        },
        now=0.0,
    )
    emitted.extend(
        projector.project(
            {
                "object": "content",
                "msg_id": "msg-reasoning-delta",
                "type": "text",
                "status": "in_progress",
                "delta": True,
                "text": "正在分析",
            },
            now=0.01,
        ),
    )
    emitted.extend(
        projector.project(
            {
                "object": "content",
                "msg_id": "msg-reasoning-delta",
                "type": "text",
                "status": "in_progress",
                "delta": True,
                "text": "用户问题。",
            },
            now=0.02,
        ),
    )

    assert emitted == [
        {"event": "answer.thinking", "text": "正在分析"},
        {"event": "answer.thinking", "text": "用户问题。"},
    ]


def test_projection_appends_real_agentscope_text_deltas() -> None:
    projector = _runtime_projector()
    emitted: list[dict] = []

    for index, text in enumerate("今天是七月"):
        emitted.extend(
            projector.project(
                AgentScopeTextContent(
                    status="in_progress",
                    delta=True,
                    text=text,
                ),
                now=0.001 * index,
            ),
        )
    emitted.extend(projector.finish(success=True, now=0.01))

    chunks = [
        item["text"]
        for item in emitted
        if item["event"] == "answer.chunk"
    ]
    assert "".join(chunks) == "今天是七月"
    assert projector.full_text == "今天是七月"


def test_projection_streams_reasoning_and_suppresses_other_internal_parent_messages() -> None:
    projector = _runtime_projector()
    emitted: list[dict] = []
    internal_parents = [
        AgentScopeMessage(
            id="msg-reasoning",
            type="reasoning",
            role="assistant",
            status="in_progress",
        ),
        AgentScopeMessage(
            id="msg-component-call",
            type="component_call",
            role="assistant",
            status="in_progress",
        ),
        AgentScopeMessage(
            id="msg-tool-role",
            type="message",
            role="tool",
            status="in_progress",
        ),
    ]
    internal_children = [
        AgentScopeTextContent(
            msg_id="msg-reasoning",
            delta=True,
            text="VISIBLE_REASONING",
        ),
        AgentScopeTextContent(
            msg_id="msg-component-call",
            delta=True,
            text="SECRET_COMPONENT_RESULT",
        ),
        AgentScopeTextContent(
            msg_id="msg-tool-role",
            delta=True,
            text="SECRET_TOOL_RESULT",
        ),
    ]

    for index, event in enumerate(internal_parents + internal_children):
        emitted.extend(projector.project(event, now=0.001 * index))

    emitted.extend(
        projector.project(
            AgentScopeMessage(
                id="msg-answer",
                type="message",
                role="assistant",
                status="in_progress",
            ),
            now=0.01,
        ),
    )
    for index, text in enumerate("正常答案"):
        emitted.extend(
            projector.project(
                AgentScopeTextContent(
                    msg_id="msg-answer",
                    delta=True,
                    text=text,
                ),
                now=0.011 + 0.001 * index,
            ),
        )
    emitted.extend(projector.finish(success=True, now=0.02))
    serialized = json.dumps(emitted, ensure_ascii=False)
    chunks = [
        item["text"]
        for item in emitted
        if item["event"] == "answer.chunk"
    ]
    thinking = [
        item["text"]
        for item in emitted
        if item["event"] == "answer.thinking"
    ]

    assert "".join(chunks) == "正常答案"
    assert thinking == ["VISIBLE_REASONING"]
    assert projector.full_text == "正常答案"
    assert "SECRET_COMPONENT_RESULT" not in serialized
    assert "SECRET_TOOL_RESULT" not in serialized


def test_runtime_projection_flushes_by_deadline_and_caps_chunk_size() -> None:
    projector = _runtime_projector(
        min_chunk_chars=128,
        max_chunk_chars=512,
        max_chunk_delay_seconds=0.075,
    )

    first = projector.project(
        {
            "object": "content",
            "type": "text",
            "status": "inprogress",
            "text": "短答案",
        },
        now=1.0,
    )
    before_deadline = projector.project(
        {
            "object": "content",
            "type": "text",
            "status": "inprogress",
            "text": "短答案",
        },
        now=1.05,
    )
    at_deadline = projector.project(
        {
            "object": "content",
            "type": "text",
            "status": "inprogress",
            "text": "短答案" + "乙" * 1200,
        },
        now=1.08,
    )
    tail = projector.finish(success=True, now=1.09)

    assert [item["event"] for item in first] == ["status.changed"]
    assert before_deadline == []
    chunks = [
        item["text"]
        for item in at_deadline + tail
        if item["event"] == "answer.chunk"
    ]
    assert "".join(chunks) == "短答案" + "乙" * 1200
    assert all(0 < len(chunk) <= 512 for chunk in chunks)


def test_runtime_projection_uses_internal_event_as_flush_clock_only() -> None:
    projector = _runtime_projector(
        min_chunk_chars=128,
        max_chunk_delay_seconds=0.075,
    )
    projector.project(
        {
            "object": "content",
            "type": "text",
            "status": "inprogress",
            "text": "等待定时刷新",
        },
        now=2.0,
    )

    emitted = projector.project(
        {
            "object": "message",
            "type": "reasoning",
            "status": "inprogress",
            "usage": {"input_tokens": 99},
        },
        now=2.08,
    )

    assert emitted == [{"event": "answer.chunk", "text": "等待定时刷新"}]


def test_runtime_projection_finish_flushes_short_answer_tail() -> None:
    projector = _runtime_projector()

    emitted = projector.project(
        {
            "object": "content",
            "type": "text",
            "status": "completed",
            "text": "简短回答",
        },
        now=0.0,
    )
    emitted.extend(projector.finish(success=True, now=0.01))

    assert emitted == [
        {
            "event": "status.changed",
            "status": "answer.generating",
            "message": "正在生成回答",
        },
        {"event": "answer.chunk", "text": "简短回答"},
        {
            "event": "answer.completed",
            "status": "completed",
            "message": "答案已生成",
        },
    ]


@pytest.mark.parametrize(
    ("terminal_event", "success", "expected_event", "expected_message"),
    [
        (
            {"object": "response", "status": "completed"},
            True,
            "answer.completed",
            "答案已生成",
        ),
        (
            {
                "object": "response",
                "status": "failed",
                "error": {"message": "secret=/tmp/private"},
            },
            False,
            "answer.failed",
            "回答生成失败",
        ),
        (
            {"object": "response", "status": "cancelled"},
            False,
            "answer.failed",
            "任务已取消",
        ),
    ],
)
def test_runtime_projection_handles_native_terminal_once(
    terminal_event: dict,
    success: bool,
    expected_event: str,
    expected_message: str,
) -> None:
    projector = _runtime_projector()

    emitted = projector.project(terminal_event, now=1.0)
    emitted.extend(projector.finish(success=success, now=1.1))
    emitted.extend(projector.finish(success=not success, now=1.2))

    terminals = [
        item
        for item in emitted
        if item["event"] in {"answer.completed", "answer.failed"}
    ]
    assert terminals == [
        {
            "event": expected_event,
            "status": "completed" if success else "failed",
            "message": expected_message,
        },
    ]


def test_projection_finishes_without_native_response() -> None:
    projector = _runtime_projector()

    emitted = projector.finish(success=True, now=1.0)

    assert emitted == [
        {
            "event": "answer.completed",
            "status": "completed",
            "message": "答案已生成",
        },
    ]


def test_runtime_projection_payload_excludes_native_sensitive_fields() -> None:
    projector = _runtime_projector()
    sensitive = {
        "native_object": {"debug": True},
        "usage": {"input_tokens": 999},
        "tool_result": "PRIVATE-TOOL-RESULT",
        "local_path": "/tmp/qwenpaw/task-secret/image.png",
        "oss_locator": "oss://private-bucket/user-secret/image.png",
        "credentials": {"access_key_secret": "SECRET-CREDENTIAL"},
    }

    emitted = projector.project(
        {
            "object": "content",
            "type": "text",
            "status": "completed",
            "text": "安全回答",
            **sensitive,
        },
        now=0.0,
    )
    emitted.extend(projector.finish(success=True, now=0.01))
    serialized = json.dumps(emitted, ensure_ascii=False)

    assert {item["event"] for item in emitted} <= {
        "status.changed",
        "answer.chunk",
        "answer.completed",
        "answer.failed",
    }
    assert all(
        set(item) <= {"event", "status", "message", "text"}
        for item in emitted
    )
    for secret in (
        "native_object",
        "usage",
        "PRIVATE-TOOL-RESULT",
        "/tmp/qwenpaw",
        "oss://",
        "SECRET-CREDENTIAL",
    ):
        assert secret not in serialized


def test_projection_activation_requires_runtime_identity() -> None:
    from qwenpaw.app.channels.console.runtime_event_projection import (
        should_project_runtime_events,
    )

    assert should_project_runtime_events(
        "bank-runtime",
        {"runtime_task_id": "task-trusted-001"},
    )
    assert not should_project_runtime_events("bank-runtime", {})
    assert not should_project_runtime_events(
        "bank-runtime",
        {"runtime_task_id": "   "},
    )
    assert not should_project_runtime_events(
        "console",
        {"runtime_task_id": "task-forged-001"},
    )
    assert not should_project_runtime_events(
        "console",
        {"runtime_tool_gateway": {"enabled": True}},
    )
    assert not should_project_runtime_events("web", {})


@pytest.mark.asyncio
async def test_generic_console_stream_bypasses_runtime_projection() -> None:
    channel = ConsoleChannel(
        process=lambda _request: None,
        enabled=True,
        bot_prefix="",
    )
    native_event = {
        "object": "message",
        "status": "inprogress",
        "type": "reasoning",
        "content": None,
        "usage": {"input_tokens": 7},
    }

    async def fake_process(_request):
        yield _FakeEvent(native_event)

    channel._process = fake_process  # type: ignore[method-assign]
    request = SimpleNamespace(
        channel="console",
        session_id="generic-session-001",
        user_id="u001",
        channel_meta={"runtime_task_id": "task-forged-001"},
        input=[],
    )

    events = [event async for event in channel.stream_one(request)]
    payloads = [
        json.loads(event.removeprefix("data:").strip())
        for event in events
    ]

    assert payloads == [native_event]


@pytest.mark.parametrize(
    "native_event",
    [
        {
            "object": "message",
            "type": "component_call",
            "status": "inprogress",
            "content": [{"type": "text", "text": "COMPONENT-CALL-SECRET"}],
        },
        {
            "object": "message",
            "type": "component_call_output",
            "status": "completed",
            "content": [{"type": "text", "text": "COMPONENT-OUTPUT-SECRET"}],
        },
        {
            "object": "message",
            "type": "mcp_call",
            "status": "inprogress",
            "content": [{"type": "text", "text": "MCP-CALL-SECRET"}],
        },
        {
            "object": "message",
            "type": "mcp_call_output",
            "status": "completed",
            "content": [{"type": "text", "text": "MCP-OUTPUT-SECRET"}],
        },
        {
            "object": "message",
            "type": "message",
            "role": "tool",
            "status": "completed",
            "content": [{"type": "text", "text": "ROLE-TOOL-SECRET"}],
        },
    ],
)
def test_projection_filters_agentscope_tool_events(native_event: dict) -> None:
    projector = _runtime_projector()

    emitted = projector.project(native_event, now=1.0)

    assert emitted == []
    assert projector.accepted_sent is False
    assert projector.full_text == ""


@pytest.mark.parametrize(
    "native_event",
    [
        {
            "object": "message",
            "type": "mcp_list_tools",
            "role": "assistant",
            "status": "completed",
            "content": [
                {
                    "object": "content",
                    "type": "text",
                    "text": "MCP-LIST-SECRET",
                },
            ],
        },
        {
            "object": "message",
            "type": "mcp_approval_request",
            "role": "assistant",
            "status": "completed",
            "content": [
                {
                    "object": "content",
                    "type": "text",
                    "text": "APPROVAL-REQUEST-SECRET",
                },
            ],
        },
        {
            "object": "message",
            "type": "mcp_approval_response",
            "role": "assistant",
            "status": "completed",
            "content": [
                {
                    "object": "content",
                    "type": "text",
                    "text": "APPROVAL-RESPONSE-SECRET",
                },
            ],
        },
        {
            "object": "message",
            "type": "error",
            "role": "assistant",
            "status": "completed",
            "content": [
                {
                    "object": "content",
                    "type": "text",
                    "text": "Traceback /tmp/private SECRET-CREDENTIAL",
                },
            ],
        },
        {
            "object": "content",
            "type": "text",
            "status": "failed",
            "error": {
                "message": "Traceback /tmp/private CONTENT-ERROR-SECRET",
            },
            "text": "Traceback /tmp/private CONTENT-ERROR-SECRET",
        },
        {
            "object": "message",
            "type": "heartbeat",
            "role": "assistant",
            "status": "inprogress",
            "content": [
                {
                    "object": "content",
                    "type": "text",
                    "text": "HEARTBEAT-SECRET",
                },
            ],
        },
        {
            "object": "message",
            "type": "future_unknown_control",
            "role": "assistant",
            "status": "completed",
            "content": [
                {
                    "object": "content",
                    "type": "text",
                    "text": "UNKNOWN-SECRET",
                },
            ],
        },
    ],
)
def test_runtime_projection_rejects_non_visible_control_events_by_default(
    native_event: dict,
) -> None:
    projector = _runtime_projector()

    emitted = projector.project(native_event, now=1.0)

    assert emitted == []
    assert projector.accepted_sent is False
    assert projector.full_text == ""


def test_projection_converts_internal_error_to_safe_failure() -> None:
    projector = _runtime_projector()

    emitted = projector.project(
        {
            "object": "response",
            "type": "error",
            "status": "failed",
            "error": {
                "message": "Traceback /tmp/private SECRET-CREDENTIAL",
            },
        },
        now=1.0,
    )
    serialized = json.dumps(emitted, ensure_ascii=False)

    assert emitted == [
        {
            "event": "answer.failed",
            "status": "failed",
            "message": "回答生成失败",
        },
    ]
    assert "Traceback" not in serialized
    assert "/tmp/private" not in serialized
    assert "SECRET-CREDENTIAL" not in serialized


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_status", ["rejected", "incomplete"])
async def test_bank_runtime_rejected_or_incomplete_emits_one_failed_terminal(
    terminal_status: str,
) -> None:
    channel = ConsoleChannel(
        process=lambda _request: None,
        enabled=True,
        bot_prefix="",
    )

    async def fake_process(_request):
        yield _FakeEvent(
            {
                "object": "response",
                "status": terminal_status,
                "type": "response",
                "output": None,
            },
        )

    channel._process = fake_process  # type: ignore[method-assign]
    request = SimpleNamespace(
        channel="bank-runtime",
        session_id=f"runtime-session-{terminal_status}",
        user_id="u001",
        channel_meta={"runtime_task_id": f"task-runtime-{terminal_status}"},
        input=[],
    )

    events = [event async for event in channel.stream_one(request)]
    payloads = [
        json.loads(event.removeprefix("data:").strip())
        for event in events
    ]

    assert payloads == [
        {
            "event": "answer.failed",
            "status": "failed",
            "message": "回答生成失败",
        },
    ]


@pytest.mark.asyncio
async def test_bank_runtime_stream_projects_only_compact_events() -> None:
    channel = ConsoleChannel(
        process=lambda _request: None,
        enabled=True,
        bot_prefix="",
    )

    async def fake_process(_request):
        for index in range(3750):
            yield _FakeEvent(
                {
                    "object": "message",
                    "status": "inprogress",
                    "type": "reasoning",
                    "sequence_number": index,
                    "usage": {"input_tokens": index},
                },
            )
        yield _FakeEvent(
            {
                "object": "content",
                "status": "completed",
                "type": "text",
                "text": "流式回答",
            },
        )
        yield _FakeEvent(
            {
                "object": "response",
                "status": "completed",
                "type": "response",
                "output": None,
            },
        )

    channel._process = fake_process  # type: ignore[method-assign]
    request = SimpleNamespace(
        channel="bank-runtime",
        session_id="runtime-session-compact",
        user_id="u001",
        channel_meta={"runtime_task_id": "task-runtime-compact"},
        input=[],
    )

    events = [event async for event in channel.stream_one(request)]
    payloads = [
        json.loads(event.removeprefix("data:").strip())
        for event in events
    ]

    assert [payload["event"] for payload in payloads] == [
        "status.changed",
        "answer.chunk",
        "answer.completed",
    ]
    assert all(
        set(payload) <= {"event", "status", "message", "text"}
        for payload in payloads
    )


@pytest.mark.asyncio
async def test_runtime_flushes_short_answer_while_idle(monkeypatch) -> None:
    flush_calls = 0
    original_flush_due = RuntimeEventProjector.flush_due

    def count_flush_due(self, **kwargs):
        nonlocal flush_calls
        flush_calls += 1
        return original_flush_due(self, **kwargs)

    monkeypatch.setattr(RuntimeEventProjector, "flush_due", count_flush_due)
    channel = ConsoleChannel(
        process=lambda _request: None,
        enabled=True,
        bot_prefix="",
    )

    async def fake_process(_request):
        yield _FakeEvent(
            {
                "object": "content",
                "status": "inprogress",
                "type": "text",
                "text": "无需等待终态",
            },
        )
        await asyncio.sleep(0.2)
        yield _FakeEvent(
            {
                "object": "response",
                "status": "completed",
                "type": "response",
                "output": None,
            },
        )

    channel._process = fake_process  # type: ignore[method-assign]
    request = SimpleNamespace(
        channel="bank-runtime",
        session_id="runtime-session-idle-flush",
        user_id="u001",
        channel_meta={"runtime_task_id": "task-runtime-idle-flush"},
        input=[],
    )
    loop = asyncio.get_running_loop()
    started_at = loop.time()
    chunk_received_at = None

    async for event in channel.stream_one(request):
        payload = json.loads(event.removeprefix("data:").strip())
        if (
            payload.get("event") == "answer.chunk"
            and chunk_received_at is None
        ):
            chunk_received_at = loop.time()

    assert chunk_received_at is not None
    assert chunk_received_at - started_at < 0.1
    assert flush_calls == 1


@pytest.mark.asyncio
async def test_runtime_empty_buffer_does_not_poll(monkeypatch) -> None:
    flush_calls = 0
    original_flush_due = RuntimeEventProjector.flush_due

    def count_flush_due(self, **kwargs):
        nonlocal flush_calls
        flush_calls += 1
        return original_flush_due(self, **kwargs)

    monkeypatch.setattr(RuntimeEventProjector, "flush_due", count_flush_due)
    channel = ConsoleChannel(
        process=lambda _request: None,
        enabled=True,
        bot_prefix="",
    )

    async def fake_process(_request):
        await asyncio.sleep(0.2)
        yield _FakeEvent(
            {
                "object": "response",
                "status": "completed",
                "type": "response",
                "output": None,
            },
        )

    channel._process = fake_process  # type: ignore[method-assign]
    request = SimpleNamespace(
        channel="bank-runtime",
        session_id="runtime-session-empty-wait",
        user_id="u001",
        channel_meta={"runtime_task_id": "task-runtime-empty-wait"},
        input=[],
    )

    payloads = [
        json.loads(event.removeprefix("data:").strip())
        async for event in channel.stream_one(request)
    ]

    assert flush_calls == 0
    assert payloads == [
        {
            "event": "answer.completed",
            "status": "completed",
            "message": "答案已生成",
        },
    ]


@pytest.mark.asyncio
async def test_runtime_terminal_closes_upstream_before_trailing() -> None:
    upstream_closed = asyncio.Event()
    trailing_event_consumed = False
    channel = ConsoleChannel(
        process=lambda _request: None,
        enabled=True,
        bot_prefix="",
    )

    async def fake_process(_request):
        nonlocal trailing_event_consumed
        try:
            yield _FakeEvent(
                {
                    "object": "response",
                    "status": "completed",
                    "type": "response",
                    "output": None,
                },
            )
            trailing_event_consumed = True
            yield _FakeEvent(
                {
                    "object": "content",
                    "status": "inprogress",
                    "type": "text",
                    "text": "TRAILING-SECRET",
                },
            )
        finally:
            upstream_closed.set()

    channel._process = fake_process  # type: ignore[method-assign]
    request = SimpleNamespace(
        channel="bank-runtime",
        session_id="runtime-session-terminal-close",
        user_id="u001",
        channel_meta={"runtime_task_id": "task-runtime-terminal-close"},
        input=[],
    )

    payloads = [
        json.loads(event.removeprefix("data:").strip())
        async for event in channel.stream_one(request)
    ]

    assert upstream_closed.is_set()
    assert trailing_event_consumed is False
    assert payloads == [
        {
            "event": "answer.completed",
            "status": "completed",
            "message": "答案已生成",
        },
    ]


@pytest.mark.asyncio
async def test_runtime_cancel_closes_upstream_without_pending() -> None:
    upstream_closed = asyncio.Event()
    release_upstream = asyncio.Event()
    channel = ConsoleChannel(
        process=lambda _request: None,
        enabled=True,
        bot_prefix="",
    )

    async def fake_process(_request):
        try:
            yield _FakeEvent(
                {
                    "object": "content",
                    "status": "inprogress",
                    "type": "text",
                    "text": "等待取消",
                },
            )
            await release_upstream.wait()
        finally:
            upstream_closed.set()

    channel._process = fake_process  # type: ignore[method-assign]
    request = SimpleNamespace(
        channel="bank-runtime",
        session_id="runtime-session-consumer-cancel",
        user_id="u001",
        channel_meta={"runtime_task_id": "task-runtime-consumer-cancel"},
        input=[],
    )
    baseline_tasks = set(asyncio.all_tasks())
    stream = channel.stream_one(request)
    first_event = await anext(stream)
    first_payload = json.loads(first_event.removeprefix("data:").strip())
    pending_read = asyncio.create_task(anext(stream))
    await asyncio.sleep(0.01)

    pending_read.cancel()
    await asyncio.gather(pending_read, return_exceptions=True)
    await stream.aclose()
    await asyncio.sleep(0)
    leaked_tasks = [
        task
        for task in asyncio.all_tasks()
        if task not in baseline_tasks and not task.done()
    ]

    assert first_payload["event"] == "status.changed"
    assert upstream_closed.is_set()
    assert leaked_tasks == []


@pytest.mark.asyncio
async def test_runtime_error_closes_generator_without_pending() -> None:
    upstream_closed = asyncio.Event()
    channel = ConsoleChannel(
        process=lambda _request: None,
        enabled=True,
        bot_prefix="",
    )

    async def fake_process(_request):
        try:
            yield _FakeEvent(
                {
                    "object": "content",
                    "status": "inprogress",
                    "type": "text",
                    "text": "部分回答",
                },
            )
            raise RuntimeError("Traceback /tmp/private UPSTREAM-SECRET")
        finally:
            upstream_closed.set()

    channel._process = fake_process  # type: ignore[method-assign]
    request = SimpleNamespace(
        channel="bank-runtime",
        session_id="runtime-session-upstream-error",
        user_id="u001",
        channel_meta={"runtime_task_id": "task-runtime-upstream-error"},
        input=[],
    )
    baseline_tasks = set(asyncio.all_tasks())

    payloads = [
        json.loads(event.removeprefix("data:").strip())
        async for event in channel.stream_one(request)
    ]
    await asyncio.sleep(0)
    leaked_tasks = [
        task
        for task in asyncio.all_tasks()
        if task not in baseline_tasks and not task.done()
    ]
    serialized = json.dumps(payloads, ensure_ascii=False)

    assert upstream_closed.is_set()
    assert leaked_tasks == []
    assert payloads[-1] == {
        "event": "answer.failed",
        "status": "failed",
        "message": "回答生成失败",
    }
    assert "UPSTREAM-SECRET" not in serialized


@pytest.mark.asyncio
async def test_runtime_terminal_precedes_slow_upstream_close() -> None:
    class SlowCloseEvents:
        def __init__(self) -> None:
            self.emitted = False
            self.closed = asyncio.Event()

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.emitted:
                await asyncio.Event().wait()
            self.emitted = True
            return _FakeEvent(
                {
                    "object": "response",
                    "status": "completed",
                    "type": "response",
                    "output": None,
                },
            )

        async def aclose(self) -> None:
            await asyncio.sleep(0.25)
            self.closed.set()

    upstream = SlowCloseEvents()
    channel = ConsoleChannel(
        process=lambda _request: upstream,
        enabled=True,
        bot_prefix="",
    )
    request = SimpleNamespace(
        channel="bank-runtime",
        session_id="runtime-session-slow-close",
        user_id="u001",
        channel_meta={"runtime_task_id": "task-runtime-slow-close"},
        input=[],
    )
    stream = channel.stream_one(request)
    loop = asyncio.get_running_loop()
    started_at = loop.time()

    terminal_event = await asyncio.wait_for(anext(stream), timeout=0.4)
    terminal_elapsed = loop.time() - started_at
    terminal_payload = json.loads(
        terminal_event.removeprefix("data:").strip(),
    )

    assert terminal_elapsed <= 0.05
    assert terminal_payload["event"] == "answer.completed"
    assert not upstream.closed.is_set()

    await stream.aclose()
    assert upstream.closed.is_set()


@pytest.mark.asyncio
async def test_runtime_close_error_cannot_swallow_terminal(caplog) -> None:
    class CloseErrorEvents:
        def __init__(self) -> None:
            self.emitted = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.emitted:
                raise StopAsyncIteration
            self.emitted = True
            return _FakeEvent(
                {
                    "object": "response",
                    "status": "completed",
                    "type": "response",
                    "output": None,
                },
            )

        async def aclose(self) -> None:
            raise RuntimeError("UPSTREAM-ACLOSE-SECRET")

    upstream = CloseErrorEvents()
    channel = ConsoleChannel(
        process=lambda _request: upstream,
        enabled=True,
        bot_prefix="",
    )
    request = SimpleNamespace(
        channel="bank-runtime",
        session_id="runtime-session-close-error",
        user_id="u001",
        channel_meta={"runtime_task_id": "task-runtime-close-error"},
        input=[],
    )
    stream = channel.stream_one(request)

    terminal_event = await anext(stream)
    terminal_payload = json.loads(
        terminal_event.removeprefix("data:").strip(),
    )
    await stream.aclose()

    assert terminal_payload["event"] == "answer.completed"
    assert "runtime native event iterator aclose failed" in caplog.text


@pytest.mark.asyncio
async def test_runtime_disconnect_after_status_closes_upstream() -> None:
    upstream_closed = asyncio.Event()
    release_upstream = asyncio.Event()
    channel = ConsoleChannel(
        process=lambda _request: None,
        enabled=True,
        bot_prefix="",
    )

    async def fake_process(_request):
        try:
            yield _FakeEvent(
                {
                    "object": "content",
                    "status": "inprogress",
                    "type": "text",
                    "text": "等待客户端断开",
                },
            )
            await release_upstream.wait()
        finally:
            upstream_closed.set()

    channel._process = fake_process  # type: ignore[method-assign]
    request = SimpleNamespace(
        channel="bank-runtime",
        session_id="runtime-session-status-disconnect",
        user_id="u001",
        channel_meta={
            "runtime_task_id": "task-runtime-status-disconnect",
        },
        input=[],
    )
    baseline_tasks = set(asyncio.all_tasks())
    stream = channel.stream_one(request)

    status_event = await anext(stream)
    status_payload = json.loads(
        status_event.removeprefix("data:").strip(),
    )
    await stream.aclose()
    await asyncio.sleep(0)
    leaked_tasks = [
        task
        for task in asyncio.all_tasks()
        if task not in baseline_tasks and not task.done()
    ]

    assert status_payload["event"] == "status.changed"
    assert upstream_closed.is_set()
    assert leaked_tasks == []


@pytest.mark.asyncio
async def test_runtime_pending_cleanup_error_is_logged(caplog) -> None:
    upstream_blocked = asyncio.Event()
    release_upstream = asyncio.Event()
    channel = ConsoleChannel(
        process=lambda _request: None,
        enabled=True,
        bot_prefix="",
    )

    async def fake_process(_request):
        yield _FakeEvent(
            {
                "object": "content",
                "status": "inprogress",
                "type": "text",
                "text": "等待取消并清理",
            },
        )
        upstream_blocked.set()
        try:
            await release_upstream.wait()
        finally:
            raise RuntimeError("PENDING-CLEANUP-SECRET")

    channel._process = fake_process  # type: ignore[method-assign]
    request = SimpleNamespace(
        channel="bank-runtime",
        session_id="runtime-session-pending-cleanup",
        user_id="u001",
        channel_meta={
            "runtime_task_id": "task-runtime-pending-cleanup",
        },
        input=[],
    )
    baseline_tasks = set(asyncio.all_tasks())
    stream = channel.stream_one(request)
    status_event = await anext(stream)
    pending_read = asyncio.create_task(anext(stream))
    await asyncio.wait_for(upstream_blocked.wait(), timeout=0.1)

    pending_read.cancel()
    await asyncio.gather(pending_read, return_exceptions=True)
    await stream.aclose()
    await asyncio.sleep(0)
    leaked_tasks = [
        task
        for task in asyncio.all_tasks()
        if task not in baseline_tasks and not task.done()
    ]

    status_payload = json.loads(
        status_event.removeprefix("data:").strip(),
    )
    assert status_payload["event"] == "status.changed"
    assert "runtime pending native event cleanup failed" in caplog.text
    assert leaked_tasks == []


@pytest.mark.asyncio
async def test_runtime_close_error_does_not_replace_main_error(caplog) -> None:
    class MainAndCloseErrorEvents:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise RuntimeError("PRIMARY-UPSTREAM-SECRET")

        async def aclose(self) -> None:
            raise RuntimeError("SECONDARY-ACLOSE-SECRET")

    upstream = MainAndCloseErrorEvents()
    channel = ConsoleChannel(
        process=lambda _request: upstream,
        enabled=True,
        bot_prefix="",
    )
    request = SimpleNamespace(
        channel="bank-runtime",
        session_id="runtime-session-main-close-error",
        user_id="u001",
        channel_meta={
            "runtime_task_id": "task-runtime-main-close-error",
        },
        input=[],
    )

    payloads = [
        json.loads(event.removeprefix("data:").strip())
        async for event in channel.stream_one(request)
    ]

    assert payloads == [
        {
            "event": "answer.failed",
            "status": "failed",
            "message": "回答生成失败",
        },
    ]
    assert "PRIMARY-UPSTREAM-SECRET" in caplog.text
    assert "SECONDARY-ACLOSE-SECRET" in caplog.text


@pytest.mark.asyncio
async def test_runtime_cancel_during_slow_terminal_close_propagates() -> None:
    class SlowCloseEvents:
        def __init__(self) -> None:
            self.emitted = False
            self.close_started = asyncio.Event()
            self.closed = asyncio.Event()

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.emitted:
                raise StopAsyncIteration
            self.emitted = True
            return _FakeEvent(
                {
                    "object": "response",
                    "status": "completed",
                    "type": "response",
                    "output": None,
                },
            )

        async def aclose(self) -> None:
            self.close_started.set()
            await asyncio.sleep(0.25)
            self.closed.set()

    upstream = SlowCloseEvents()
    channel = ConsoleChannel(
        process=lambda _request: upstream,
        enabled=True,
        bot_prefix="",
    )
    request = SimpleNamespace(
        channel="bank-runtime",
        session_id="runtime-session-cancel-slow-close",
        user_id="u001",
        channel_meta={
            "runtime_task_id": "task-runtime-cancel-slow-close",
        },
        input=[],
    )
    baseline_tasks = set(asyncio.all_tasks())
    stream = channel.stream_one(request)
    terminal_event = await anext(stream)
    terminal_payload = json.loads(
        terminal_event.removeprefix("data:").strip(),
    )
    close_task = asyncio.create_task(stream.aclose())
    await asyncio.wait_for(upstream.close_started.wait(), timeout=0.1)

    close_task.cancel()
    await asyncio.gather(close_task, return_exceptions=True)
    await asyncio.sleep(0)
    leaked_tasks = [
        task
        for task in asyncio.all_tasks()
        if task not in baseline_tasks and not task.done()
    ]

    assert terminal_payload["event"] == "answer.completed"
    assert close_task.cancelled()
    assert upstream.closed.is_set()
    assert leaked_tasks == []


@pytest.mark.asyncio
async def test_runtime_pending_cleanup_recancel_propagates() -> None:
    upstream_blocked = asyncio.Event()
    cleanup_started = asyncio.Event()
    upstream_closed = asyncio.Event()
    release_upstream = asyncio.Event()
    channel = ConsoleChannel(
        process=lambda _request: None,
        enabled=True,
        bot_prefix="",
    )

    async def fake_process(_request):
        yield _FakeEvent(
            {
                "object": "content",
                "status": "inprogress",
                "type": "text",
                "text": "等待重复取消",
            },
        )
        upstream_blocked.set()
        try:
            await release_upstream.wait()
        finally:
            cleanup_started.set()
            await asyncio.sleep(0.25)
            upstream_closed.set()

    channel._process = fake_process  # type: ignore[method-assign]
    request = SimpleNamespace(
        channel="bank-runtime",
        session_id="runtime-session-pending-recancel",
        user_id="u001",
        channel_meta={
            "runtime_task_id": "task-runtime-pending-recancel",
        },
        input=[],
    )
    baseline_tasks = set(asyncio.all_tasks())
    stream = channel.stream_one(request)
    status_event = await anext(stream)
    status_payload = json.loads(
        status_event.removeprefix("data:").strip(),
    )
    pending_read = asyncio.create_task(anext(stream))
    await asyncio.wait_for(upstream_blocked.wait(), timeout=0.1)

    pending_read.cancel()
    await asyncio.wait_for(cleanup_started.wait(), timeout=0.1)
    pending_read.cancel()
    await asyncio.gather(pending_read, return_exceptions=True)
    await stream.aclose()
    await asyncio.sleep(0)
    leaked_tasks = [
        task
        for task in asyncio.all_tasks()
        if task not in baseline_tasks and not task.done()
    ]

    assert status_payload["event"] == "status.changed"
    assert pending_read.cancelled()
    assert upstream_closed.is_set()
    assert leaked_tasks == []
