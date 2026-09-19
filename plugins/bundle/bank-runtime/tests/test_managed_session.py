from __future__ import annotations

import asyncio
import copy
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from bank_runtime.session import (
    ManagedSessionCleanupHook,
    ManagedSessionCommitHook,
    ManagedSessionDisableLongTermMemoryHook,
    ManagedSessionError,
    ManagedSessionErrorHook,
    ManagedSessionPrepareHook,
    ManagedSessionStore,
    current_managed_session_scope,
)
from qwenpaw.agents.middlewares import MemoryMiddleware
from qwenpaw.app.chats.session import SafeJSONSession
from qwenpaw.hooks.session.session_hook import SessionLoadHook, SessionSaveHook
from qwenpaw.runtime.hooks import HookRegistry
from qwenpaw.runtime.phases import Phase
from qwenpaw.runtime.runtime import Runtime


class _Agent:
    def __init__(self, state):
        self.state = copy.deepcopy(state)

    def state_dict(self):
        return copy.deepcopy(self.state)


class _MemoryManager:
    def get_memory_prompt(self):
        return "LONG-TERM-MEMORY-GUIDANCE"

    async def memory_search(self):
        return None

    def list_memory_tools(self):
        return [self.memory_search]


class _ReadOnlySessionWorkspace:
    """Match QwenPaw 2.1 Workspace's service-backed session property."""

    def __init__(self, session):
        self._service_manager = SimpleNamespace(services={"session": session})

    @property
    def session(self):
        return self._service_manager.services.get("session")


def _request(
    *,
    user_id="user-a",
    task_id="task-001",
    session_state="uninitialized",
    operation="append",
    bootstrap=None,
    regenerate_from_task_id=None,
):
    values = {
        "session_id": "session-001",
        "user_id": user_id,
        "channel": "bank-runtime",
        "runtime_task_id": task_id,
        "qwenpaw_session_state": session_state,
        "session_contract_version": "2.0",
        "session_operation": operation,
    }
    if bootstrap is not None:
        values["session_bootstrap"] = bootstrap
    if regenerate_from_task_id is not None:
        values["regenerate_from_task_id"] = regenerate_from_task_id
    return SimpleNamespace(**values)


def _ctx(
    session,
    *,
    agent_id="assistant-a",
    request=None,
    agent=None,
):
    return SimpleNamespace(
        request=request or _request(),
        workspace=SimpleNamespace(session=session),
        session_id="session-001",
        agent_id=agent_id,
        session_state=None,
        mode_state={},
        extras={},
        agent=agent,
        error=None,
    )


async def _prepare_and_load(ctx):
    await ManagedSessionPrepareHook().run(ctx)
    await SessionLoadHook().run(ctx)


async def _commit_and_cleanup(ctx):
    await ManagedSessionCommitHook().run(ctx)
    await SessionSaveHook().run(ctx)
    await ManagedSessionCleanupHook().run(ctx)


@pytest.mark.asyncio
async def test_active_missing_session_fails_with_stable_error_code(tmp_path):
    ctx = _ctx(
        SafeJSONSession(str(tmp_path)),
        request=_request(session_state="active"),
    )

    with pytest.raises(ManagedSessionError) as raised:
        await ManagedSessionPrepareHook().run(ctx)

    assert raised.value.error_code == "RUNTIME_SESSION_NOT_FOUND"
    assert "path" not in str(raised.value).lower()
    await ManagedSessionCleanupHook().run(ctx)
    assert current_managed_session_scope() is None


@pytest.mark.asyncio
async def test_prepare_supports_qwenpaw_21_read_only_session_property(tmp_path):
    delegate = SafeJSONSession(str(tmp_path))
    ctx = _ctx(delegate)
    ctx.workspace = _ReadOnlySessionWorkspace(delegate)

    await ManagedSessionPrepareHook().run(ctx)

    assert isinstance(ctx.workspace.session, ManagedSessionStore)
    assert ctx.workspace.session.delegate is delegate
    await ManagedSessionCleanupHook().run(ctx)


@pytest.mark.asyncio
async def test_bootstrap_preserves_roles_and_ignores_non_text_content(tmp_path):
    ctx = _ctx(
        SafeJSONSession(str(tmp_path)),
        request=_request(
            bootstrap={
                "version": "2.0",
                "messages": [
                    {
                        "role": "system",
                        "content": "must not load",
                    },
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "first question"},
                            {"type": "file", "url": "file:///secret"},
                        ],
                    },
                    {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "first answer"}],
                    },
                ],
            }
        ),
    )

    await _prepare_and_load(ctx)

    context = ctx.session_state["state"]["context"]
    assert [message["role"] for message in context] == ["user", "assistant"]
    assert len(context[0]["content"]) == 1
    assert context[0]["content"][0]["type"] == "text"
    assert context[0]["content"][0]["text"] == "first question"
    await ManagedSessionCleanupHook().run(ctx)


@pytest.mark.asyncio
async def test_existing_session_ignores_bootstrap_and_binds_full_scope(tmp_path):
    delegate = SafeJSONSession(str(tmp_path))
    first = _ctx(delegate, agent=_Agent({"state": {"context": []}}))
    await _prepare_and_load(first)
    first.agent.state = {
        "state": {
            "context": [
                {"role": "user", "content": [{"type": "text", "text": "saved"}]},
                {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
            ]
        }
    }
    await _commit_and_cleanup(first)

    second = _ctx(
        first.workspace.session,
        request=_request(
            task_id="task-002",
            session_state="active",
            bootstrap={
                "version": "2.0",
                "messages": [{"role": "user", "content": "injected"}],
            },
        ),
    )
    await _prepare_and_load(second)

    assert second.session_state["state"]["context"][0]["content"][0]["text"] == "saved"
    state = await delegate.get_session_state_dict(
        session_id="session-001",
        user_id="user-a",
        channel="bank-runtime",
    )
    assert state["bank_runtime_scope"] == {
        "agent_id": "assistant-a",
        "user_id": "user-a",
        "channel": "bank-runtime",
        "session_id": "session-001",
        "last_committed_task_id": "task-001",
    }
    await ManagedSessionCleanupHook().run(second)


@pytest.mark.asyncio
async def test_stale_session_rebuilds_from_runtime_bootstrap(tmp_path):
    delegate = SafeJSONSession(str(tmp_path))
    legacy_agent = SimpleNamespace(
        state_dict=lambda: {
            "state": {
                "context": [
                    {"role": "user", "content": [{"type": "text", "text": "legacy"}]}
                ]
            }
        }
    )
    await delegate.save_session_state(
        session_id="session-001",
        user_id="user-a",
        channel="bank-runtime",
        agent=legacy_agent,
    )
    ctx = _ctx(
        delegate,
        request=_request(
            task_id="task-recovered",
            session_state="stale",
            bootstrap={
                "version": "2.0",
                "messages": [
                    {"role": "user", "content": "runtime-authorized history"},
                    {"role": "assistant", "content": "restored answer"},
                ],
            },
        ),
    )

    await _prepare_and_load(ctx)

    context = ctx.session_state["state"]["context"]
    assert [message["role"] for message in context] == ["user", "assistant"]
    assert context[0]["content"][0]["text"] == "runtime-authorized history"
    await ManagedSessionCleanupHook().run(ctx)


@pytest.mark.asyncio
async def test_stale_session_requires_valid_runtime_bootstrap(tmp_path):
    delegate = SafeJSONSession(str(tmp_path))
    ctx = _ctx(
        delegate,
        request=_request(session_state="stale", bootstrap={"version": "2.0"}),
    )

    with pytest.raises(ManagedSessionError) as raised:
        await ManagedSessionPrepareHook().run(ctx)

    assert raised.value.error_code == "RUNTIME_SESSION_REQUEST_INVALID"
    await ManagedSessionCleanupHook().run(ctx)


@pytest.mark.asyncio
async def test_cross_user_or_agent_scope_cannot_load_copied_state(tmp_path):
    delegate = SafeJSONSession(str(tmp_path))
    first = _ctx(delegate, agent=_Agent({"state": {"context": []}}))
    await _prepare_and_load(first)
    await _commit_and_cleanup(first)
    stored = await delegate.get_session_state_dict(
        session_id="session-001",
        user_id="user-a",
        channel="bank-runtime",
    )
    copied = SimpleNamespace(
        state_dict=lambda: copy.deepcopy(stored["agent"]),
    )
    copied_scope = SimpleNamespace(
        state_dict=lambda: copy.deepcopy(stored["bank_runtime_scope"]),
    )
    await delegate.save_session_state(
        session_id="session-001",
        user_id="user-b",
        channel="bank-runtime",
        agent=copied,
        bank_runtime_scope=copied_scope,
    )

    wrong_user = _ctx(
        first.workspace.session,
        request=_request(user_id="user-b", session_state="active"),
    )
    with pytest.raises(ManagedSessionError) as raised:
        await ManagedSessionPrepareHook().run(wrong_user)
    assert raised.value.error_code == "RUNTIME_SESSION_SCOPE_MISMATCH"
    await ManagedSessionCleanupHook().run(wrong_user)

    wrong_agent = _ctx(
        first.workspace.session,
        agent_id="assistant-b",
        request=_request(session_state="active"),
    )
    with pytest.raises(ManagedSessionError) as raised:
        await ManagedSessionPrepareHook().run(wrong_agent)
    assert raised.value.error_code == "RUNTIME_SESSION_SCOPE_MISMATCH"
    await ManagedSessionCleanupHook().run(wrong_agent)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_kind", ["model", "tool", "attachment", "cancel"])
async def test_failure_and_cancel_paths_never_commit_partial_turn(
    tmp_path,
    failure_kind,
):
    del failure_kind
    delegate = SafeJSONSession(str(tmp_path))
    ctx = _ctx(delegate, agent=_Agent({"state": {"context": [{"role": "user"}]}}))
    await _prepare_and_load(ctx)

    await ctx.workspace.session.save_session_state(
        session_id="session-001",
        user_id="user-a",
        channel="bank-runtime",
        agent=SimpleNamespace(state_dict=ctx.agent.state_dict),
    )

    assert (
        await delegate.get_session_state_dict(
            session_id="session-001",
            user_id="user-a",
            channel="bank-runtime",
        )
        == {}
    )
    await ManagedSessionCleanupHook().run(ctx)


@pytest.mark.asyncio
async def test_upstream_cancel_save_is_suppressed_for_managed_session(tmp_path):
    delegate = SafeJSONSession(str(tmp_path))
    ctx = _ctx(
        delegate,
        agent=_Agent({"state": {"context": [{"role": "user"}]}}),
    )
    await _prepare_and_load(ctx)

    await Runtime(workspace=ctx.workspace, app_services=None)._try_save_on_cancel(ctx)

    assert (
        await delegate.get_session_state_dict(
            session_id="session-001",
            user_id="user-a",
            channel="bank-runtime",
        )
        == {}
    )
    await ManagedSessionCleanupHook().run(ctx)


@pytest.mark.asyncio
async def test_bank_runtime_removes_long_term_memory_from_request_agent(tmp_path):
    memory_manager = _MemoryManager()
    memory_middleware = MemoryMiddleware(memory_manager=memory_manager)
    ordinary_middleware = object()
    memory_tool = SimpleNamespace(name="memory_search")
    ordinary_tool = SimpleNamespace(name="ordinary_tool")
    agent = _Agent({})
    agent._system_prompt = "ordinary prompt\n\nLONG-TERM-MEMORY-GUIDANCE"
    agent.toolkit = SimpleNamespace(
        tool_groups=[
            SimpleNamespace(tools=[memory_tool, ordinary_tool]),
        ]
    )
    for attribute in (
        "_reply_middlewares",
        "_reasoning_middlewares",
        "_acting_middlewares",
        "_model_call_middlewares",
        "_system_prompt_middlewares",
        "_compress_context_middlewares",
    ):
        setattr(agent, attribute, [memory_middleware, ordinary_middleware])
    ctx = _ctx(SafeJSONSession(str(tmp_path)), agent=agent)
    ctx.workspace.memory_manager = memory_manager

    await ManagedSessionDisableLongTermMemoryHook().run(ctx)

    assert agent.toolkit.tool_groups[0].tools == [ordinary_tool]
    assert agent._system_prompt == "ordinary prompt"
    for attribute in (
        "_reply_middlewares",
        "_reasoning_middlewares",
        "_acting_middlewares",
        "_model_call_middlewares",
        "_system_prompt_middlewares",
        "_compress_context_middlewares",
    ):
        assert getattr(agent, attribute) == [ordinary_middleware]
    assert memory_manager.list_memory_tools()[0].__name__ == "memory_search"


@pytest.mark.asyncio
async def test_non_bank_channel_keeps_long_term_memory(tmp_path):
    memory_manager = _MemoryManager()
    memory_middleware = MemoryMiddleware(memory_manager=memory_manager)
    memory_tool = SimpleNamespace(name="memory_search")
    agent = _Agent({})
    agent._system_prompt = "ordinary prompt\n\nLONG-TERM-MEMORY-GUIDANCE"
    agent.toolkit = SimpleNamespace(tool_groups=[SimpleNamespace(tools=[memory_tool])])
    agent._reply_middlewares = [memory_middleware]
    request = _request()
    request.channel = "console"
    ctx = _ctx(SafeJSONSession(str(tmp_path)), request=request, agent=agent)
    ctx.workspace.memory_manager = memory_manager

    await ManagedSessionDisableLongTermMemoryHook().run(ctx)

    assert agent.toolkit.tool_groups[0].tools == [memory_tool]
    assert agent._system_prompt.endswith("LONG-TERM-MEMORY-GUIDANCE")
    assert agent._reply_middlewares == [memory_middleware]


@pytest.mark.asyncio
async def test_success_commit_sanitizes_runtime_attachment_and_locator(tmp_path):
    delegate = SafeJSONSession(str(tmp_path))
    ctx = _ctx(
        delegate,
        agent=_Agent(
            {
                "state": {
                    "context": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": "describe it"},
                                {
                                    "type": "file",
                                    "url": "file:///task-files/task-001/secret.pdf",
                                    "locator": "oss://private/object",
                                    "_runtime_sandbox_attachment": True,
                                },
                                {
                                    "type": "text",
                                    "text": (
                                        '<runtime_attachment file_ref="fr1_'
                                        + "a" * 64
                                        + "_"
                                        + "b" * 64
                                        + '">private metadata</runtime_attachment>'
                                    ),
                                },
                            ],
                        },
                        {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "done"}],
                        },
                    ]
                }
            }
        ),
    )
    await _prepare_and_load(ctx)

    await _commit_and_cleanup(ctx)

    state = await delegate.get_session_state_dict(
        session_id="session-001",
        user_id="user-a",
        channel="bank-runtime",
    )
    serialized = str(state)
    assert "file://" not in serialized
    assert "oss://" not in serialized
    assert "_runtime_sandbox_attachment" not in serialized
    assert "fr1_" not in serialized
    assert state["agent"]["state"]["context"][0]["content"] == [
        {"type": "text", "text": "describe it"}
    ]


@pytest.mark.asyncio
async def test_regenerate_replaces_last_turn_and_same_task_retry_is_idempotent(
    tmp_path,
):
    delegate = SafeJSONSession(str(tmp_path))
    first = _ctx(delegate)
    first.request = _request(task_id="task-old")
    first.agent = _Agent(
        {
            "state": {
                "context": [
                    {"role": "user", "content": [{"type": "text", "text": "old q"}]},
                    {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "old a"}],
                    },
                ]
            }
        }
    )
    await _prepare_and_load(first)
    await _commit_and_cleanup(first)

    regenerate = _ctx(
        first.workspace.session,
        request=_request(
            task_id="task-new",
            session_state="active",
            operation="regenerate",
            regenerate_from_task_id="task-old",
        ),
    )
    await _prepare_and_load(regenerate)
    assert regenerate.session_state["state"]["context"] == []
    regenerate.agent = _Agent(
        {
            "state": {
                "context": [
                    {"role": "user", "content": [{"type": "text", "text": "old q"}]},
                    {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "new a"}],
                    },
                ]
            }
        }
    )
    await _commit_and_cleanup(regenerate)

    retry = _ctx(
        regenerate.workspace.session,
        request=_request(task_id="task-new", session_state="active"),
        agent=_Agent(
            {
                "state": {
                    "context": [
                        {
                            "role": "user",
                            "content": [{"type": "text", "text": "duplicate"}],
                        },
                    ]
                }
            }
        ),
    )
    await _prepare_and_load(retry)
    await _commit_and_cleanup(retry)

    stored = await delegate.get_session_state_dict(
        session_id="session-001",
        user_id="user-a",
        channel="bank-runtime",
    )
    assert stored["bank_runtime_scope"]["last_committed_task_id"] == "task-new"
    assert [
        item["content"][0]["text"] for item in stored["agent"]["state"]["context"]
    ] == ["old q", "new a"]


@pytest.mark.asyncio
async def test_cleanup_releases_lock_and_clears_request_context(tmp_path):
    ctx = _ctx(SafeJSONSession(str(tmp_path)))
    await ManagedSessionPrepareHook().run(ctx)
    scope = current_managed_session_scope()
    assert scope is not None and scope.lock.locked()

    await ManagedSessionCleanupHook().run(ctx)

    assert current_managed_session_scope() is None
    assert not scope.lock.locked()
    assert not any(key.startswith("bank_runtime_session") for key in ctx.extras)


def test_plugin_session_hooks_order_around_builtin_load_and_save():
    registry = HookRegistry()
    for hook in (
        SessionLoadHook(),
        SessionSaveHook(),
        ManagedSessionPrepareHook(),
        ManagedSessionDisableLongTermMemoryHook(),
        ManagedSessionCommitHook(),
        ManagedSessionErrorHook(),
        ManagedSessionCleanupHook(),
    ):
        registry.register(hook)

    assert [hook.name for hook in registry.hooks_for(Phase.PRE_AGENT_BUILD)] == [
        "bank_runtime_session_prepare",
        "session_load",
    ]
    assert [hook.name for hook in registry.hooks_for(Phase.POST_AGENT_BUILD)] == [
        "bank_runtime_disable_long_term_memory",
    ]
    assert [hook.name for hook in registry.hooks_for(Phase.POST_RESPONSE)] == [
        "bank_runtime_session_commit",
        "session_save",
    ]


@pytest.mark.asyncio
async def test_same_scoped_session_is_serialized(tmp_path):
    first = _ctx(SafeJSONSession(str(tmp_path)))
    await ManagedSessionPrepareHook().run(first)
    second = _ctx(
        first.workspace.session,
        request=_request(task_id="task-002"),
    )
    acquired = asyncio.Event()
    release = asyncio.Event()

    async def second_request():
        await ManagedSessionPrepareHook().run(second)
        acquired.set()
        await release.wait()
        await ManagedSessionCleanupHook().run(second)

    waiting = asyncio.create_task(second_request())
    await asyncio.sleep(0)

    assert not waiting.done()
    await ManagedSessionCleanupHook().run(first)
    await acquired.wait()
    assert current_managed_session_scope() is None
    release.set()
    await waiting


@pytest.mark.asyncio
async def test_cancel_while_waiting_does_not_release_another_request_lock(
    tmp_path,
):
    first = _ctx(SafeJSONSession(str(tmp_path)))
    await ManagedSessionPrepareHook().run(first)
    first_scope = current_managed_session_scope()
    second = _ctx(
        first.workspace.session,
        request=_request(task_id="task-002"),
    )

    async def wait_for_same_session():
        try:
            await ManagedSessionPrepareHook().run(second)
        finally:
            await ManagedSessionCleanupHook().run(second)

    waiting = asyncio.create_task(wait_for_same_session())
    await asyncio.sleep(0)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting

    assert first_scope is not None
    assert first_scope.lock.locked()
    await ManagedSessionCleanupHook().run(first)


@pytest.mark.asyncio
async def test_error_hook_restores_stable_session_code_after_generic_mapping(
    tmp_path,
):
    ctx = _ctx(SafeJSONSession(str(tmp_path)))
    ctx.error = ManagedSessionError("RUNTIME_SESSION_NOT_FOUND")
    ctx.extras = {
        "_error_code": "UNKNOWN_AGENT_ERROR",
        "_error_text": "unsafe generic detail",
    }

    await ManagedSessionErrorHook().run(ctx)

    assert ctx.extras["_error_code"] == "RUNTIME_SESSION_NOT_FOUND"
    assert ctx.extras["_error_text"] == "Managed session is unavailable"
