# -*- coding: utf-8 -*-
"""Runtime-managed conversations must remain external to QwenPaw storage."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from agentscope.message import Msg

from qwenpaw.app.routers import console as console_router
from qwenpaw.app.runner import runner as runner_module
from qwenpaw.app.runner.runner import AgentRunner
from qwenpaw.config.context import (
    get_current_runtime_attachments_manifest,
    get_current_runtime_discovered_file_ids,
    get_current_runtime_sandbox_context,
    get_current_runtime_tool_gateway,
    get_current_session_id,
    get_current_toolkit,
)


class _RuntimeConsoleChannel:
    @staticmethod
    def resolve_session_id(*, sender_id, channel_meta):
        del sender_id
        return channel_meta["session_id"]

    async def stream_one(self, _payload):
        yield 'data: {"status": "completed", "object": "response"}\n\n'


@pytest.mark.asyncio
async def test_runtime_console_does_not_register_chat_or_generate_title(
    monkeypatch,
) -> None:
    channel = _RuntimeConsoleChannel()
    workspace = SimpleNamespace(
        channel_manager=SimpleNamespace(
            get_channel=AsyncMock(return_value=channel),
        ),
        chat_manager=SimpleNamespace(
            get_or_create_chat=AsyncMock(),
        ),
        task_tracker=MagicMock(),
    )
    monkeypatch.setattr(
        console_router,
        "get_agent_for_request",
        AsyncMock(return_value=workspace),
    )
    title = AsyncMock()
    monkeypatch.setattr(console_router, "generate_and_update_title", title)
    payload = {
        "channel": "bank-runtime",
        "user_id": "opaque-subject",
        "session_id": "qpaw_scoped_session",
        "runtime_task_id": "task_001",
        "sandbox_context": {"task_id": "task_001"},
        "input": [
            {
                "role": "user",
                "content": [{"type": "text", "text": "private message"}],
            },
        ],
    }

    response = await console_router.post_console_chat(payload, MagicMock())
    events = [event async for event in response.body_iterator]

    assert events == [
        'data: {"status": "completed", "object": "response"}\n\n',
    ]
    workspace.chat_manager.get_or_create_chat.assert_not_awaited()
    title.assert_not_awaited()
    assert not workspace.task_tracker.mock_calls


class _FakeAgent:
    def __init__(self, **_kwargs) -> None:
        self.toolkit = SimpleNamespace(skills={})
        self.memory = None
        self.model = None
        self.persist_calls = 0

    async def register_mcp_clients(self) -> None:
        return None

    async def __call__(self, _msgs):
        return Msg(name="assistant", role="assistant", content="ok")

    def set_console_output_enabled(self, *, enabled: bool) -> None:
        del enabled

    def rebuild_sys_prompt(self) -> None:
        return None

    async def prepare_personal_skills_for_persistence(self) -> None:
        self.persist_calls += 1

    async def interrupt(self) -> None:
        return None


@pytest.mark.asyncio
async def test_runtime_runner_uses_context_manifest_without_local_session_state(
    monkeypatch,
) -> None:
    fake_agent = _FakeAgent()
    monkeypatch.setattr(
        runner_module,
        "QwenPawAgent",
        lambda **kwargs: fake_agent,
    )
    config = SimpleNamespace(
        name="Bank assistant",
        running=SimpleNamespace(
            shell_command_executable=None,
        ),
        coding_mode=None,
        plan=SimpleNamespace(enabled=False),
    )
    monkeypatch.setattr(runner_module, "load_agent_config", lambda _agent_id: config)
    monkeypatch.setattr(
        runner_module,
        "build_env_context",
        lambda **_kwargs: {},
    )

    async def no_mission(**_kwargs):
        raise AssertionError("Runtime external mode must not inspect mission state")

    monkeypatch.setattr(runner_module, "maybe_handle_mission_command", no_mission)
    monkeypatch.setattr(
        runner_module,
        "detect_active_mission_phase",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Runtime external mode must not inspect mission state"),
        ),
    )

    async def fake_stream(*, coroutine_task, **_kwargs):
        response = await coroutine_task
        yield response, True

    monkeypatch.setattr(
        runner_module,
        "_stream_printing_messages_interruptible",
        fake_stream,
    )

    from qwenpaw.observability import langfuse as langfuse_module

    @asynccontextmanager
    async def trace_scope(**_kwargs):
        yield None

    monkeypatch.setattr(langfuse_module, "agent_trace_scope", trace_scope)

    runner = AgentRunner(agent_id="bank-assistant")
    runner.session = SimpleNamespace(
        get_session_state_dict=AsyncMock(),
        load_session_state=AsyncMock(),
        save_session_state=AsyncMock(),
        update_session_state=AsyncMock(),
    )
    runner._chat_manager = SimpleNamespace(
        get_or_create_chat=AsyncMock(),
        touch_chat=AsyncMock(),
    )
    request = SimpleNamespace(
        session_id="qpaw_scoped_session",
        user_id="opaque-subject",
        channel="bank-runtime",
        channel_meta={
            "runtime_task_id": "task_001",
            "attachments_manifest": [{"file_id": "file_001"}],
            "sandbox_context": {"task_id": "task_001"},
            "runtime_tool_gateway": {"endpoint": "http://runtime.invalid"},
        },
        request_context={},
        root_session_id="",
        model_extra={},
    )
    messages = [Msg(name="user", role="user", content="private message")]

    events = [
        event
        async for event in runner.query_handler(messages, request=request)
    ]

    assert len(events) == 1
    runner._chat_manager.get_or_create_chat.assert_not_awaited()
    runner._chat_manager.touch_chat.assert_not_awaited()
    runner.session.get_session_state_dict.assert_not_awaited()
    runner.session.load_session_state.assert_not_awaited()
    runner.session.save_session_state.assert_not_awaited()
    runner.session.update_session_state.assert_not_awaited()
    assert fake_agent.persist_calls == 0


def test_runtime_request_context_is_empty_after_reply_scope() -> None:
    assert get_current_toolkit() is None
    assert get_current_session_id() is None
    assert get_current_runtime_tool_gateway() is None
    assert get_current_runtime_attachments_manifest() is None
    assert get_current_runtime_sandbox_context() is None
    assert get_current_runtime_discovered_file_ids() == frozenset()
