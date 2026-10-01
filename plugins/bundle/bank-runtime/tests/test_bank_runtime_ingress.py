from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from bank_runtime.events import CompactEventProjector, project_sse_stream
from bank_runtime.delivery_state import current_delivery_state
from bank_runtime.router import build_ingress_router
from bank_runtime.channel import BankRuntimeChannel
from qwenpaw.app.channels.console.channel import ConsoleChannel
from qwenpaw.app.task_tracker import TaskTracker


def _request_body(**overrides):
    body = {
        "runtime_task_id": "task-001",
        "session_id": "session-001",
        "user_id": "user-001",
        "channel": "bank-runtime",
        "input": [
            {
                "role": "user",
                "content": [{"type": "text", "text": "当前问题"}],
            }
        ],
        "sandbox_context": {"task_id": "task-001"},
    }
    body.update(overrides)
    return body


def _headers(token: str = "candidate-secret", agent_id: str = "assistant-a"):
    return {
        "Authorization": f"Bearer {token}",
        "X-Agent-Id": agent_id,
    }


class _FakeChannel:
    channel = "bank-runtime"

    def __init__(self, raw_events=None):
        self.requests = []
        self.raw_events = raw_events or [
            {
                "object": "response",
                "status": "completed",
            }
        ]

    async def stream_one(self, request):
        self.requests.append(request)
        for event in self.raw_events:
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


@pytest.mark.asyncio
async def test_operations_facts_use_private_metadata_envelope_and_preserve_answer():
    from qwenpaw.drivers.handlers.mcp_stateful_client import HttpStatefulClient

    client = HttpStatefulClient("bank:mcp", "streamable_http", "http://local.invalid")
    client.is_connected = True

    class Session:
        async def call_tool(self, name, arguments, **kwargs):
            assert arguments == {"secret": "secret argument"}
            return type("Result", (), {"isError": False})()

    client.session = Session()

    async def source():
        state = current_delivery_state()
        assert state is not None
        state.operations_events.append({
            "event_type": "personal_skill.activated",
            "skill_id": "skill_001",
            "version_no": 3,
            "content_hash": "a" * 64,
            "result": "activated",
            "duration_bucket": "lt_100ms",
        })
        await client.call_tool("document/read_chunks", {"secret": "secret argument"})
        yield 'data: {"object":"response","status":"completed"}\n\n'

    output = [json.loads(item.removeprefix("data: ")) async for item in
              project_sse_stream(source(), "task-001", "trace-001")]
    facts = [item["metadata"]["runtime_event"] for item in output if "metadata" in item]
    assert [item["event_type"] for item in facts] == [
        "personal_skill.activated", "mcp.request.completed",
    ]
    assert facts[1]["task_id"] == "task-001"
    assert facts[1]["trace_id"] == "trace-001"
    assert facts[1]["server_code"] == "bank:mcp"
    assert facts[1]["tool_code"] == "document/read_chunks"
    assert facts[1]["terminal_status"] == "success"
    assert "secret argument" not in str(output)
    assert "secret result" not in str(output)
    assert output[-1]["event"] == "answer.completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
async def test_completed_mcp_fact_is_drained_before_upstream_interrupt(failure):
    from qwenpaw.drivers.handlers.mcp_stateful_client import HttpStatefulClient

    client = HttpStatefulClient("bank:mcp", "streamable_http", "http://local.invalid")
    client.is_connected = True

    class Session:
        async def call_tool(self, name, arguments, **kwargs):
            return type("Result", (), {"isError": False})()

    client.session = Session()

    async def source():
        await client.call_tool("document/read_chunks", {"secret": "private-body"})
        raise failure("private-upstream-error")
        yield ""  # pragma: no cover - keeps the source an async generator

    output = []
    with pytest.raises(failure, match="private-upstream-error"):
        async for item in project_sse_stream(source(), "task-001", "trace-001"):
            output.append(json.loads(item.removeprefix("data: ")))
    facts = [item["metadata"]["runtime_event"] for item in output if "metadata" in item]
    assert len(facts) == 1
    assert facts[0]["event_type"] == "mcp.request.completed"
    assert facts[0]["server_code"] == "bank:mcp"
    assert facts[0]["tool_code"] == "document/read_chunks"
    assert "private-body" not in str(output)
    assert "private-upstream-error" not in str(output)
    assert not any(item.get("event") in {"answer.completed", "answer.failed"} for item in output)


@pytest.mark.asyncio
async def test_completed_mcp_fact_is_drained_when_producer_task_is_cancelled():
    from qwenpaw.drivers.handlers.mcp_stateful_client import HttpStatefulClient

    client = HttpStatefulClient("bank:mcp", "streamable_http", "http://local.invalid")
    client.is_connected = True

    class Session:
        async def call_tool(self, name, arguments, **kwargs):
            return type("Result", (), {"isError": False})()

    client.session = Session()
    dispatched = asyncio.Event()
    output = []

    async def source():
        await client.call_tool("document/read_chunks", {})
        dispatched.set()
        await asyncio.Event().wait()
        yield ""  # pragma: no cover

    async def consume():
        async for item in project_sse_stream(source(), "task-001"):
            output.append(json.loads(item.removeprefix("data: ")))

    task = asyncio.create_task(consume())
    await asyncio.wait_for(dispatched.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    facts = [item["metadata"]["runtime_event"] for item in output if "metadata" in item]
    assert len(facts) == 1
    assert facts[0]["event_type"] == "mcp.request.completed"
    assert facts[0]["server_code"] == "bank:mcp"


def test_mcp_identifier_projection_rejects_query_and_raw_payload():
    from bank_runtime.delivery_state import DeliveryState
    from bank_runtime.events import _record_mcp_request

    state = DeliveryState("task-001")
    _record_mcp_request(state, {
        "request_id": "request-001",
        "server_code": "https://bank.example/mcp?token=private",
        "tool_code": "document/read_chunks?args=private",
        "terminal_status": "success",
        "arguments": "private-body",
    })
    event = state.operations_events[0]
    assert event["server_code"] == ""
    assert event["tool_code"] == ""
    assert "private" not in str(event)
    _record_mcp_request(state, {
        "request_id": "request-002",
        "server_code": "https://bank.example/mcp",
        "tool_code": "document/read_chunks",
        "terminal_status": "success",
    })
    assert state.operations_events[1]["server_code"] == ""
    assert state.operations_events[1]["tool_code"] == "document/read_chunks"


class _FakeChatManager:
    async def get_or_create_chat(self, session_id, user_id, channel, **kwargs):
        return SimpleNamespace(id=f"chat:{session_id}")

    async def get_chat_id_by_session(self, session_id, channel, user_id=None):
        return f"chat:{session_id}"


class _FakeChannelManager:
    def __init__(self, channel):
        self.channel = channel

    async def get_channel(self, channel):
        return self.channel if channel == "bank-runtime" else None


class _StopTracker:
    def __init__(self):
        self.stopped = []

    async def request_stop(self, chat_id):
        self.stopped.append(chat_id)
        return True


def _workspace(channel=None, tracker=None):
    channel = channel or _FakeChannel()
    return SimpleNamespace(
        channel_manager=_FakeChannelManager(channel),
        chat_manager=_FakeChatManager(),
        task_tracker=tracker or TaskTracker(),
    )


def _client(monkeypatch, workspace):
    monkeypatch.setenv("QWENPAW_SERVICE_TOKEN", "candidate-secret")

    async def _get_workspace(request, agent_id=None):
        return workspace

    monkeypatch.setattr(
        "bank_runtime.router.get_agent_for_request",
        _get_workspace,
    )
    app = FastAPI()
    app.include_router(build_ingress_router(), prefix="/api/bank-runtime")
    return TestClient(app)


def test_bank_runtime_channel_is_independent_and_constructible(tmp_path):
    async def process(request):
        if False:
            yield request

    channel = BankRuntimeChannel.from_config(
        process=process,
        config=SimpleNamespace(
            enabled=True,
            bot_prefix="",
            media_dir="",
        ),
        workspace_dir=tmp_path,
    )

    assert channel.channel == "bank-runtime"
    assert ConsoleChannel.channel == "console"
    assert channel.enabled is True


@pytest.mark.parametrize(
    "headers",
    [
        {"X-Agent-Id": "assistant-a"},
        _headers(token="wrong-secret"),
        {"Authorization": "Bearer candidate-secret"},
    ],
)
def test_chat_rejects_missing_or_untrusted_service_identity(
    monkeypatch,
    headers,
):
    response = _client(monkeypatch, _workspace()).post(
        "/api/bank-runtime/agents/assistant-a/chat",
        headers=headers,
        json=_request_body(),
    )

    assert response.status_code == 401
    assert "candidate-secret" not in response.text
    assert "wrong-secret" not in response.text


def test_agent_path_and_header_must_match(monkeypatch):
    response = _client(monkeypatch, _workspace()).post(
        "/api/bank-runtime/agents/assistant-b/chat",
        headers=_headers(agent_id="assistant-a"),
        json=_request_body(),
    )

    assert response.status_code == 401


def test_plugin_health_is_authenticated_and_agent_scoped(monkeypatch):
    client = _client(monkeypatch, _workspace())

    accepted = client.get(
        "/api/bank-runtime/agents/assistant-a/health",
        headers=_headers(),
    )
    rejected = client.get(
        "/api/bank-runtime/agents/assistant-b/health",
        headers=_headers(agent_id="assistant-a"),
    )

    assert accepted.status_code == 200
    assert accepted.json() == {
        "status": "ok",
        "channel": "bank-runtime",
        "plugin_version": "0.7.0",
    }
    assert rejected.status_code == 401


@pytest.mark.parametrize(
    "body",
    [
        _request_body(runtime_task_id=""),
        _request_body(session_id=""),
        _request_body(sandbox_context={"task_id": "another-task"}),
    ],
)
def test_chat_rejects_missing_or_mismatched_runtime_context(
    monkeypatch,
    body,
):
    response = _client(monkeypatch, _workspace()).post(
        "/api/bank-runtime/agents/assistant-a/chat",
        headers=_headers(),
        json=body,
    )

    assert response.status_code == 400


def test_chat_accepts_exactly_one_current_user_message(monkeypatch):
    channel = _FakeChannel()
    client = _client(monkeypatch, _workspace(channel=channel))
    multi_role = _request_body(
        input=[
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "历史回答"}],
            },
            {
                "role": "user",
                "content": [{"type": "text", "text": "当前问题"}],
            },
        ]
    )

    rejected = client.post(
        "/api/bank-runtime/agents/assistant-a/chat",
        headers=_headers(),
        json=multi_role,
    )
    accepted = client.post(
        "/api/bank-runtime/agents/assistant-a/chat",
        headers=_headers(),
        json=_request_body(),
    )

    assert rejected.status_code == 400
    assert accepted.status_code == 200
    assert len(channel.requests) == 1
    assert len(channel.requests[0].input) == 1
    assert str(channel.requests[0].input[0].role).lower().endswith("user")


def _response_events(response):
    return [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]


def test_stream_projects_incremental_thinking_and_answer_with_one_terminal(
    monkeypatch,
):
    channel = _FakeChannel(
        [
            {
                "object": "message",
                "id": "thinking-1",
                "type": "reasoning",
                "status": "in_progress",
                "content": "思",
            },
            {
                "object": "message",
                "id": "thinking-1",
                "type": "reasoning",
                "status": "in_progress",
                "content": "思考",
            },
            {
                "object": "message",
                "id": "answer-1",
                "type": "message",
                "status": "in_progress",
                "content": "答",
            },
            {
                "object": "message",
                "id": "answer-1",
                "type": "message",
                "status": "completed",
                "content": "答案",
            },
            {"object": "response", "status": "completed"},
        ]
    )
    response = _client(monkeypatch, _workspace(channel=channel)).post(
        "/api/bank-runtime/agents/assistant-a/chat",
        headers=_headers(),
        json=_request_body(),
    )

    assert response.status_code == 200
    events = _response_events(response)
    assert [event["event"] for event in events] == [
        "status.changed",
        "answer.chunk",
        "answer.completed",
    ]
    assert [event.get("text") for event in events if event['event'] == 'answer.chunk'] == ["答案"]
    assert not any(event['event'] == 'answer.thinking' for event in events)
    assert (
        sum(event["event"] in {"answer.completed", "answer.failed"} for event in events)
        == 1
    )


def test_stream_projects_qwenpaw_21_boolean_delta_text_chunks(monkeypatch):
    channel = _FakeChannel(
        [
            {
                "object": "content",
                "type": "text",
                "status": "in_progress",
                "delta": True,
                "msg_id": "answer-1",
                "index": 0,
                "text": "你",
            },
            {
                "object": "content",
                "type": "text",
                "status": "in_progress",
                "delta": True,
                "msg_id": "answer-1",
                "index": 0,
                "text": "好",
            },
            {
                "object": "content",
                "type": "text",
                "status": "completed",
                "delta": False,
                "msg_id": "answer-1",
                "index": 0,
                "text": "你好",
            },
            {"object": "response", "status": "completed"},
        ]
    )
    response = _client(monkeypatch, _workspace(channel=channel)).post(
        "/api/bank-runtime/agents/assistant-a/chat",
        headers=_headers(),
        json=_request_body(),
    )

    assert response.status_code == 200
    events = _response_events(response)
    assert [event["event"] for event in events] == [
        "status.changed",
        "answer.chunk",
        "answer.completed",
    ]
    assert events[1]["text"] == "你好"


def test_stream_keeps_reasoning_content_deltas_out_of_answer_chunks(monkeypatch):
    channel = _FakeChannel(
        [
            {
                "object": "message",
                "id": "thinking-1",
                "type": "reasoning",
                "status": "in_progress",
                "content": None,
            },
            {
                "object": "content",
                "type": "text",
                "status": None,
                "delta": True,
                "msg_id": "thinking-1",
                "text": "思",
            },
            {
                "object": "content",
                "type": "text",
                "status": None,
                "delta": True,
                "msg_id": "thinking-1",
                "text": "考",
            },
            {
                "object": "message",
                "id": "thinking-1",
                "type": "reasoning",
                "status": "completed",
                "content": "思考",
            },
            {"object": "response", "status": "completed"},
        ]
    )
    response = _client(monkeypatch, _workspace(channel=channel)).post(
        "/api/bank-runtime/agents/assistant-a/chat",
        headers=_headers(),
        json=_request_body(),
    )

    assert response.status_code == 200
    events = _response_events(response)
    assert [event["event"] for event in events] == [
        "status.changed",
        "answer.completed",
    ]
    assert not any(event['event'] == 'answer.thinking' for event in events)
    assert not any(event["event"] == "answer.chunk" for event in events)


def test_stream_keeps_plugin_call_arguments_out_of_answer_chunks(monkeypatch):
    channel = _FakeChannel(
        [
            {
                "object": "message",
                "id": "plugin-call-1",
                "type": "plugin_call",
                "status": "in_progress",
                "content": None,
            },
            {
                "object": "content",
                "type": "text",
                "status": None,
                "delta": True,
                "msg_id": "plugin-call-1",
                "text": "# 不应显示的文件正文",
            },
            {
                "object": "message",
                "id": "plugin-call-1",
                "type": "plugin_call",
                "status": "completed",
                "content": "# 不应显示的文件正文",
            },
            {"object": "response", "status": "completed"},
        ]
    )
    response = _client(monkeypatch, _workspace(channel=channel)).post(
        "/api/bank-runtime/agents/assistant-a/chat",
        headers=_headers(),
        json=_request_body(),
    )

    assert response.status_code == 200
    events = _response_events(response)
    assert [event["event"] for event in events] == [
        "status.changed",
        "answer.completed",
    ]


def test_stop_resolves_only_the_bank_runtime_session(monkeypatch):
    tracker = _StopTracker()
    response = _client(
        monkeypatch,
        _workspace(tracker=tracker),
    ).post(
        "/api/bank-runtime/agents/assistant-a/chat/stop",
        params={"chat_id": "session-001"},
        headers=_headers(),
        json={
            "runtime_task_id": "task-001",
            "session_id": "session-001",
            "reason": "user_cancelled",
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "runtime_task_id": "task-001",
        "stopped": True,
        "stop_status": "stopped",
    }
    assert tracker.stopped == ["chat:session-001"]


def test_stop_rejects_query_and_body_session_mismatch(monkeypatch):
    response = _client(monkeypatch, _workspace()).post(
        "/api/bank-runtime/agents/assistant-a/chat/stop",
        params={"chat_id": "another-session"},
        headers=_headers(),
        json={
            "runtime_task_id": "task-001",
            "session_id": "session-001",
        },
    )

    assert response.status_code == 400


def test_projector_flushes_final_snapshot_and_suppresses_duplicate_terminal():
    projector = CompactEventProjector("task-001")

    projected = []
    projected += projector.project(
        {
            "object": "message",
            "id": "answer-1",
            "type": "message",
            "status": "completed",
            "content": "最终正文",
        }
    )
    projected += projector.project({"object": "response", "status": "completed"})
    projected += projector.project({"event": "answer.failed", "status": "failed"})
    projected += projector.finish()

    assert projected == [
        {"event": "answer.chunk", "text": "最终正文", "message_id": "answer-1"},
        {
            "event": "answer.completed",
            "status": "completed",
            "message": "回答完成",
        },
    ]


@pytest.mark.asyncio
async def test_disconnect_detaches_subscriber_without_cancelling_run():
    release = asyncio.Event()

    async def raw_stream():
        yield 'data: {"event":"answer.chunk","text":"首段"}\n\n'
        await release.wait()
        yield 'data: {"event":"answer.completed","status":"completed"}\n\n'

    tracker = TaskTracker()
    queue, _ = await tracker.attach_or_start(
        "chat-001",
        None,
        lambda _: project_sse_stream(raw_stream(), "task-001"),
    )
    subscriber = tracker.stream_from_queue(queue, "chat-001")

    accepted = await anext(subscriber)
    first = await anext(subscriber)
    await subscriber.aclose()

    assert "status.changed" in accepted
    assert "answer.chunk" in first
    assert await tracker.get_status("chat-001") == "running"
    reconnected = await tracker.attach("chat-001")
    assert reconnected is not None
    release.set()
    replay = []
    async for item in tracker.stream_from_queue(reconnected, "chat-001"):
        replay.append(item)
    assert any("answer.completed" in item for item in replay)


@pytest.mark.asyncio
async def test_projector_emits_one_sanitized_failure_on_stream_error():
    projector = CompactEventProjector("task-001")
    events = projector.project({"error": "sensitive upstream detail"})
    events += projector.finish()

    assert events == [
        {
            "event": "answer.failed",
            "status": "failed",
            "message": "回答生成失败",
            "error_code": "WORKER_FAILED",
        }
    ]


def test_projector_preserves_only_recoverable_session_codes():
    missing = CompactEventProjector("task-001").project(
        {
            "object": "response",
            "status": "failed",
            "error": {
                "code": "RUNTIME_SESSION_NOT_FOUND",
                "message": "/private/session/path must not leak",
            },
        }
    )
    internal = CompactEventProjector("task-001").project(
        {
            "object": "response",
            "status": "failed",
            "error": {
                "code": "INTERNAL_SECRET_CODE",
                "message": "sensitive detail",
            },
        }
    )
    scope_mismatch = CompactEventProjector("task-001").project(
        {
            "object": "response",
            "status": "failed",
            "error": {
                "code": "RUNTIME_SESSION_SCOPE_MISMATCH",
                "message": "/private/session/path must not leak",
            },
        }
    )

    assert missing == [
        {
            "event": "answer.failed",
            "status": "failed",
            "message": "回答生成失败",
            "error_code": "RUNTIME_SESSION_NOT_FOUND",
        }
    ]
    assert internal == [
        {
            "event": "answer.failed",
            "status": "failed",
            "message": "回答生成失败",
            "error_code": "WORKER_FAILED",
        }
    ]
    assert scope_mismatch == [
        {
            "event": "answer.failed",
            "status": "failed",
            "message": "回答生成失败",
            "error_code": "RUNTIME_SESSION_SCOPE_MISMATCH",
        }
    ]


@pytest.mark.asyncio
async def test_cancelled_native_stream_cannot_report_completed():
    entered = asyncio.Event()
    output = []
    async def source():
        entered.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            # Native cleanup may finish the stream with a response envelope.
            yield 'data: {"object":"response","status":"completed"}\n\n'
    async def collect():
        async for item in project_sse_stream(source(), "cancel-task"):
            output.extend(json.loads(line[6:]) for line in item.splitlines() if line.startswith("data: "))
    task = asyncio.create_task(collect())
    await entered.wait()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert not any(e["event"] == "answer.completed" for e in output)
    assert any(e.get("error_code") == "QWENPAW_TASK_CANCELLED" for e in output)
def test_only_registered_layout_marker_survives_error_projection():
    from bank_runtime.events import CompactEventProjector
    for message, expected in [
        ("PPTX_LAYOUT_CAPACITY|5|chart_conclusion", "PPTX_LAYOUT_CAPACITY|5|chart_conclusion"),
        ("PPTX_LAYOUT_CAPACITY|0|text", "回答生成失败"),
        ("PPTX_LAYOUT_CAPACITY|5|text\nprivate", "回答生成失败"),
        ("/private/source", "回答生成失败"),
    ]:
        result = CompactEventProjector("t").project({"event": "error", "error": {"code": "ARTIFACT_VALIDATION_FAILED", "message": message}})
        assert result[0]["message"] == expected


@pytest.mark.parametrize("user_text", ["", "   ", "请总结附件"])
def test_real_channel_dispatches_each_attachment_request_without_text_debounce(monkeypatch, user_text):
    received = []

    async def process(request):
        received.append(request)
        if False:
            yield request

    channel = BankRuntimeChannel.from_config(process, SimpleNamespace(enabled=True, bot_prefix="", media_dir=""))
    with _client(monkeypatch, _workspace(channel=channel)) as client:
        for index in range(2):
            response = client.post("/api/bank-runtime/agents/assistant-a/chat", headers=_headers(), json=_request_body(
                runtime_task_id=f"task-attachment-{index}",
                input=[{"role": "user", "content": [{"type": "text", "text": user_text}]}],
                sandbox_context={"task_id": f"task-attachment-{index}"},
                attachments_manifest=[{"file_id": f"file-{index}", "source": "current_task"}],
            ))
            assert response.status_code == 200
            assert len(received) == index + 1
            assert len(received[-1].input[0].content) == 1
            assert received[-1].input[0].content[0].text == user_text
    assert not channel._pending_content_by_session
