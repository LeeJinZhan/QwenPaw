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
        "answer.thinking",
        "answer.thinking",
        "answer.chunk",
        "answer.chunk",
        "answer.completed",
    ]
    assert [event.get("text") for event in events[1:5]] == [
        "思",
        "考",
        "答",
        "案",
    ]
    assert (
        sum(event["event"] in {"answer.completed", "answer.failed"} for event in events)
        == 1
    )


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
        {"event": "answer.chunk", "text": "最终正文"},
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
        }
    ]


def test_projector_preserves_only_the_stable_session_missing_code():
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
        }
    ]
