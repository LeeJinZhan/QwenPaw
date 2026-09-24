from __future__ import annotations

import hashlib
from pathlib import Path
import sys
from types import SimpleNamespace

from agentscope.message import Msg, TextBlock
import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from bank_runtime.sandbox.cache import PreparedSandboxFile
from bank_runtime.sandbox.hooks import (
    BankRuntimeAttachmentPrepareHook,
    BankRuntimeSandboxCleanupHook,
    BankRuntimeSandboxInstallHook,
)
from bank_runtime.sandbox import hooks as sandbox_hooks
from bank_runtime.sandbox import tools as sandbox_tools


class _Cache:
    def __init__(self, root: Path, events: list[str] | None = None) -> None:
        self.root = root
        self.cleaned = []
        self.events = events if events is not None else []

    async def prepare_files(self, scope, file_ids, broker, selection_records=None):
        del scope, broker, selection_records
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / "file_current.txt"
        path.write_text("untrusted attachment text", encoding="utf-8")
        return [
            PreparedSandboxFile(
                file_id=file_ids[0],
                local_path=path,
                content_type="text/plain",
                size_bytes=path.stat().st_size,
                original_name="材料.txt",
                expires_at="2099-08-19T12:00:00+08:00",
                task_id="task_001",
            )
        ]

    async def cleanup(self, task_id):
        self.cleaned.append(task_id)
        self.events.append(f"cleanup:{task_id}")


class _FileRefs:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.issued = []

    def purge_expired(self):
        self.events.append("purge")

    def issue(self, prepared, *, expires_at):
        self.issued.append((prepared.file_id, prepared.task_id, expires_at))
        self.events.append(f"issue:{prepared.file_id}")
        return f"fr1_{prepared.file_id}"

    def revoke_task(self, task_id):
        self.events.append(f"revoke:{task_id}")


def _ctx(tmp_path):
    request = SimpleNamespace(
        channel="bank-runtime",
        runtime_task_id="task_001",
        user_id="user_001",
        sandbox_context={
            "context_id": "ctx_001",
            "task_id": "task_001",
            "signature": "signed",
            "expires_at": "2099-08-19T12:00:00+08:00",
        },
        attachments_manifest=[
            {
                "file_id": "file_current",
                "source": "current_task",
                "content_hash": "sha256:" + hashlib.sha256(b"x").hexdigest(),
            }
        ],
        runtime_tool_gateway={
            "base_url": "http://127.0.0.1:8765",
            "capability_snapshot_hash": "a" * 64,
        },
        runtime_tool_visibility={
            "worker_type": "qwenpaw",
            "worker_tool_names": [
                "runtime_sandbox_files_search",
                "runtime_sandbox_files_select",
            ],
            "binding_snapshot_hash": f"sha256:{'a' * 64}",
            "authoritative": True,
        },
    )
    group = SimpleNamespace(tools=[])
    engine = SimpleNamespace()
    agent = SimpleNamespace(
        toolkit=SimpleNamespace(tool_groups=[group]),
        _system_prompt="base",
        _engine=engine,
    )
    return SimpleNamespace(
        request=request,
        agent=agent,
        input_msgs=[
            Msg(
                name="user",
                role="user",
                content=[TextBlock(type="text", text="总结附件")],
            )
        ],
        extras={},
    )


@pytest.mark.asyncio
async def test_current_attachments_are_prepared_before_execute_and_cleaned(
    tmp_path,
    monkeypatch,
) -> None:
    events: list[str] = []
    cache = _Cache(tmp_path / "task-files", events)
    file_refs = _FileRefs(events)
    monkeypatch.setenv("QWENPAW_SERVICE_TOKEN", "service-secret")
    monkeypatch.setattr(sandbox_hooks, "_CACHE", cache)
    monkeypatch.setattr(sandbox_hooks, "_FILE_REFS", file_refs)
    ctx = _ctx(tmp_path)

    await BankRuntimeSandboxInstallHook().run(ctx)
    names = [tool.name for tool in ctx.agent.toolkit.tool_groups[0].tools]
    assert names == ["runtime_sandbox_files_search", "runtime_sandbox_files_select"]
    assert "BANK RUNTIME FILE BOUNDARY" in ctx.agent._system_prompt

    await BankRuntimeAttachmentPrepareHook().run(ctx)
    rendered = "\n".join(
        block.text
        for block in ctx.input_msgs[-1].content
        if isinstance(block, TextBlock)
    )
    assert "untrusted attachment text" in rendered
    assert "trusted='false'" in rendered
    assert file_refs.issued[0][0:2] == ("file_current", "task_001")

    await BankRuntimeSandboxCleanupHook().run(ctx)
    assert cache.cleaned == ["task_001"]
    assert events[-2:] == ["revoke:task_001", "cleanup:task_001"]


@pytest.mark.asyncio
async def test_cleanup_failure_preserves_answer_and_revokes_file_refs(tmp_path, monkeypatch, caplog):
    events = []
    cache = _Cache(tmp_path / 'task-files', events)
    async def fail_cleanup(task_id):
        raise OSError('private path must not enter logs')
    cache.cleanup = fail_cleanup
    monkeypatch.setenv('QWENPAW_SERVICE_TOKEN', 'service-secret')
    monkeypatch.setattr(sandbox_hooks, '_CACHE', cache)
    monkeypatch.setattr(sandbox_hooks, '_FILE_REFS', _FileRefs(events))
    ctx = _ctx(tmp_path)
    await BankRuntimeSandboxInstallHook().run(ctx)
    await BankRuntimeSandboxCleanupHook().run(ctx)
    assert events == ['revoke:task_001']
    assert 'bank_runtime_sandbox_state_token' not in ctx.extras
    assert 'task_cache.cleanup_failed' in caplog.text
    assert 'private path' not in caplog.text


@pytest.mark.asyncio
async def test_sandbox_install_does_not_expose_unbound_file_broker_tools(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("QWENPAW_SERVICE_TOKEN", "service-secret")
    ctx = _ctx(tmp_path)
    ctx.request.runtime_tool_visibility["worker_tool_names"] = []

    await BankRuntimeSandboxInstallHook().run(ctx)

    assert ctx.agent.toolkit.tool_groups[0].tools == []


@pytest.mark.asyncio
async def test_sandbox_install_missing_gateway_leaves_no_request_state_or_tools(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("QWENPAW_SERVICE_TOKEN", "service-secret")
    ctx = _ctx(tmp_path)
    ctx.request.runtime_tool_gateway = None

    with pytest.raises(RuntimeError, match="gateway is unavailable"):
        await BankRuntimeSandboxInstallHook().run(ctx)

    assert ctx.extras == {}
    assert ctx.agent.toolkit.tool_groups[0].tools == []
    assert sandbox_tools._STATE.get() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["", "按之前要求汇总"])
async def test_attachment_turn_keeps_actual_user_text_and_guidance_in_system(tmp_path, monkeypatch, text):
    monkeypatch.setenv("QWENPAW_SERVICE_TOKEN", "service-secret")
    monkeypatch.setattr(sandbox_hooks, "_CACHE", _Cache(tmp_path / "files"))
    monkeypatch.setattr(sandbox_hooks, "_FILE_REFS", _FileRefs([]))
    ctx = _ctx(tmp_path)
    ctx.input_msgs[-1].content = [TextBlock(type="text", text=text)]
    await BankRuntimeSandboxInstallHook().run(ctx)
    try:
        assert "explicit unfinished request" in ctx.agent._system_prompt
        await BankRuntimeAttachmentPrepareHook().run(ctx)
        assert ctx.input_msgs[-1].content[0].text == text
        assert all("已收到文件，你希望我如何处理" not in getattr(block, "text", "") for block in ctx.input_msgs[-1].content)
    finally:
        await BankRuntimeSandboxCleanupHook().run(ctx)


@pytest.mark.asyncio
async def test_actual_request_conversion_retains_attachment_only_target(tmp_path, monkeypatch):
    from qwenpaw.runtime.runtime import Runtime
    from qwenpaw.schemas import AgentRequest
    source = _ctx(tmp_path)
    request = AgentRequest(**{
        **vars(source.request),
        'session_id': 'session_attachment',
        'input': [{'role': 'user', 'content': [{'type': 'text', 'text': ''}]}],
    })
    runtime = Runtime(workspace=SimpleNamespace(workspace_dir=tmp_path, agent_id='default'), app_services=None)
    ctx = runtime._build_context(request)
    ctx.agent = source.agent
    monkeypatch.setenv('QWENPAW_SERVICE_TOKEN', 'service-secret')
    monkeypatch.setattr(sandbox_hooks, '_CACHE', _Cache(tmp_path / 'files'))
    monkeypatch.setattr(sandbox_hooks, '_FILE_REFS', _FileRefs([]))
    await BankRuntimeSandboxInstallHook().run(ctx)
    try:
        await BankRuntimeAttachmentPrepareHook().run(ctx)
        assert len(ctx.input_msgs) == 1
        assert ctx.input_msgs[0].role == 'user'
        assert ctx.input_msgs[0].content[0].text == ''
        assert any('untrusted attachment text' in getattr(b, 'text', '') for b in ctx.input_msgs[0].content)
    finally:
        await BankRuntimeSandboxCleanupHook().run(ctx)


@pytest.mark.asyncio
@pytest.mark.parametrize("state,bootstrap,text,clarify", [
    ("uninitialized", None, "", True),
    ("uninitialized", None, "  ", True),
    ("active", None, "", False),
    ("uninitialized", {"messages": [{"role": "user", "content": "汇总报表"}]}, "", False),
    ("uninitialized", None, "汇总报表", False),
])
async def test_new_file_only_turn_clarifies_after_authorization_and_keeps_session(tmp_path, monkeypatch, state, bootstrap, text, clarify):
    from unittest.mock import AsyncMock
    from qwenpaw.runtime.runtime import Runtime
    from qwenpaw.schemas import AgentRequest
    from qwenpaw.runtime.hooks import HookAction
    source = _ctx(tmp_path)
    request = AgentRequest(**{**vars(source.request), 'session_id': 'session_file',
        'qwenpaw_session_state': state, 'session_bootstrap': bootstrap,
        'input': [{'role': 'user', 'content': [{'type': 'text', 'text': text}]}]})
    runtime = Runtime(workspace=SimpleNamespace(workspace_dir=tmp_path, agent_id='default'), app_services=None)
    ctx = runtime._build_context(request)
    ctx.agent = source.agent
    ctx.agent.observe = AsyncMock()
    events = []
    monkeypatch.setenv('QWENPAW_SERVICE_TOKEN', 'service-secret')
    monkeypatch.setattr(sandbox_hooks, '_CACHE', _Cache(tmp_path / 'files', events))
    refs = _FileRefs(events)
    monkeypatch.setattr(sandbox_hooks, '_FILE_REFS', refs)
    await BankRuntimeSandboxInstallHook().run(ctx)
    try:
        result = await BankRuntimeAttachmentPrepareHook().run(ctx)
        assert bool(result.action == HookAction.SHORT_CIRCUIT) == clarify
        assert refs.issued
        if clarify:
            assert result.payload.get_text_content() == '已收到文件，你希望我如何处理？'
            ctx.agent.observe.assert_awaited_once()
            observed = ctx.agent.observe.call_args.args[0]
            assert observed[0].role == 'user'
            assert observed[0].content[0].text == text
            assert observed[-1].role == 'assistant'
        else:
            ctx.agent.observe.assert_not_called()
    finally:
        await BankRuntimeSandboxCleanupHook().run(ctx)


@pytest.mark.asyncio
async def test_attachment_metadata_survives_session_sanitization_for_followup(tmp_path, monkeypatch):
    from bank_runtime.session import _sanitize_agent_state
    monkeypatch.setenv('QWENPAW_SERVICE_TOKEN', 'service-secret')
    monkeypatch.setattr(sandbox_hooks, '_CACHE', _Cache(tmp_path / 'files'))
    monkeypatch.setattr(sandbox_hooks, '_FILE_REFS', _FileRefs([]))
    ctx = _ctx(tmp_path)
    await BankRuntimeSandboxInstallHook().run(ctx)
    try:
        await BankRuntimeAttachmentPrepareHook().run(ctx)
        message = ctx.input_msgs[-1]
        stored = _sanitize_agent_state({'context': [{'role': 'user',
            'metadata': message.metadata,
            'content': [{'type': 'text', 'text': '<runtime_attachment file_ref="old-secret">private body</runtime_attachment>'}]}]})
        assert stored['context'][0]['content'] == []
        assert stored['context'][0]['metadata']['runtime_attachment_metadata'] == [
            {'file_id': 'file_current', 'display_name': '材料.txt', 'content_type': 'text/plain'}]
        assert 'private body' not in str(stored) and 'old-secret' not in str(stored)
    finally:
        await BankRuntimeSandboxCleanupHook().run(ctx)
    followup = _ctx(tmp_path)
    followup.request.attachments_manifest = []
    followup.request.runtime_task_id = 'task_followup'
    followup.request.sandbox_context['task_id'] = 'task_followup'
    followup.agent.state_dict = lambda: {"state": stored}
    await BankRuntimeSandboxInstallHook().run(followup)
    try:
        # Historical metadata must not trigger materialization in ordinary chat.
        from unittest.mock import AsyncMock
        prepare = AsyncMock(side_effect=AssertionError('unexpected historical download'))
        monkeypatch.setattr(sandbox_hooks._CACHE, 'prepare_files', prepare)
        await BankRuntimeAttachmentPrepareHook().run(followup)
        prepare.assert_not_called()
        prompt = followup.agent._system_prompt
        assert '材料.txt' in prompt
        assert 'does NOT describe earlier files' in prompt
        assert 'search/select' in prompt
        assert 'old-secret' not in prompt and 'private body' not in prompt
        assert not followup.extras[sandbox_hooks._STATE].scope.selected_file_ids
        assert not followup.extras[sandbox_hooks._STATE].scope.current_attachment_ids
    finally:
        await BankRuntimeSandboxCleanupHook().run(followup)
