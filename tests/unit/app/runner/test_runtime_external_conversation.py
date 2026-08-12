# -*- coding: utf-8 -*-
"""Runtime-managed conversations use isolated QwenPaw session state."""

from __future__ import annotations

from contextlib import asynccontextmanager
from contextvars import ContextVar, copy_context
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from agentscope.message import Msg
from agentscope.memory import InMemoryMemory

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
        self.memory = SimpleNamespace(add=AsyncMock())
        self.model = None
        self.persist_calls = 0
        self.reply_error: Exception | None = None

    async def register_mcp_clients(self) -> None:
        return None

    async def __call__(self, _msgs):
        if self.reply_error is not None:
            raise self.reply_error
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
@pytest.mark.parametrize("reply_fails", [False, True])
@pytest.mark.parametrize("session_exists", [False, True])
async def test_runtime_runner_commits_managed_session_only_on_success(
    monkeypatch,
    reply_fails: bool,
    session_exists: bool,
) -> None:
    fake_agent = _FakeAgent()
    if reply_fails:
        fake_agent.reply_error = RuntimeError("model failed")
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
    execution_lock = SimpleNamespace(
        acquire=AsyncMock(),
        release=MagicMock(),
        locked=MagicMock(return_value=True),
    )
    runner.session = SimpleNamespace(
        session_exists=AsyncMock(return_value=session_exists),
        execution_lock=MagicMock(return_value=execution_lock),
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
            "qwenpaw_session_state": (
                "active" if session_exists else "uninitialized"
            ),
            "session_bootstrap": {
                "version": "2.0",
                "messages": [
                    {
                        "role": "user",
                        "content": [{"type": "text", "text": "first question"}],
                    },
                    {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "first answer"}],
                    },
                ],
            },
        },
        request_context={},
        root_session_id="",
        model_extra={},
    )
    messages = [Msg(name="user", role="user", content="private message")]

    if reply_fails:
        with pytest.raises(Exception, match="model failed"):
            _ = [
                event
                async for event in runner.query_handler(
                    messages,
                    request=request,
                )
            ]
    else:
        events = [
            event
            async for event in runner.query_handler(messages, request=request)
        ]
        assert len(events) == 1
    runner._chat_manager.get_or_create_chat.assert_not_awaited()
    runner._chat_manager.touch_chat.assert_not_awaited()
    runner.session.get_session_state_dict.assert_not_awaited()
    runner.session.session_exists.assert_awaited_once_with(
        session_id="qpaw_scoped_session",
        user_id="opaque-subject",
        channel="bank-runtime",
    )
    if session_exists:
        runner.session.load_session_state.assert_awaited_once()
        fake_agent.memory.add.assert_not_awaited()
    else:
        runner.session.load_session_state.assert_not_awaited()
        fake_agent.memory.add.assert_awaited_once()
        restored = fake_agent.memory.add.await_args.args[0]
        assert [message.role for message in restored] == ["user", "assistant"]
    if reply_fails:
        runner.session.save_session_state.assert_not_awaited()
    else:
        runner.session.save_session_state.assert_awaited_once()
    runner.session.update_session_state.assert_not_awaited()
    runner.session.execution_lock.assert_called_once_with(
        session_id="qpaw_scoped_session",
        user_id="opaque-subject",
        channel="bank-runtime",
    )
    execution_lock.acquire.assert_awaited_once()
    execution_lock.release.assert_called_once()
    assert fake_agent.persist_calls == (0 if reply_fails else 1)


def test_runtime_request_context_is_empty_after_reply_scope() -> None:
    assert get_current_toolkit() is None
    assert get_current_session_id() is None
    assert get_current_runtime_tool_gateway() is None
    assert get_current_runtime_attachments_manifest() is None
    assert get_current_runtime_sandbox_context() is None
    assert get_current_runtime_discovered_file_ids() == frozenset()


def test_agent_context_token_can_be_restored_after_stream_context_handoff() -> None:
    from qwenpaw.app.agent_context import restore_context_token

    current_request = ContextVar("test_runtime_request", default=None)
    producer_context = copy_context()
    token = producer_context.run(current_request.set, "runtime-request")

    restore_context_token(token)

    assert current_request.get() is None


def test_agent_context_token_reuse_remains_an_error() -> None:
    from qwenpaw.app.agent_context import restore_context_token

    current_request = ContextVar("test_reused_runtime_request", default=None)
    token = current_request.set("runtime-request")
    restore_context_token(token)

    with pytest.raises(RuntimeError, match="already been used once"):
        restore_context_token(token)


@pytest.mark.asyncio
async def test_regenerate_rolls_back_only_the_last_session_turn() -> None:
    memory = InMemoryMemory()
    first_user = Msg(name="user", role="user", content="first question")
    first_answer = Msg(name="assistant", role="assistant", content="first answer")
    last_user = Msg(name="user", role="user", content="last question")
    tool = Msg(name="tool", role="system", content="tool result")
    last_answer = Msg(name="assistant", role="assistant", content="last answer")
    await memory.add([first_user, first_answer, last_user, tool, last_answer])

    removed = await runner_module._rollback_last_session_turn(memory)

    assert removed == 3
    assert [message.content for message in await memory.get_memory()] == [
        "first question",
        "first answer",
    ]
