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
        runtime_tool_gateway={"base_url": "http://127.0.0.1:8765"},
    )
    group = SimpleNamespace(tools=[])
    engine = SimpleNamespace(trusted=[])
    engine.trust_sandbox_broker_tools = engine.trusted.extend
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
    assert ctx.agent._engine.trusted == ctx.agent.toolkit.tool_groups[0].tools
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
async def test_sandbox_install_failure_leaves_no_request_state_or_tools(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("QWENPAW_SERVICE_TOKEN", "service-secret")
    ctx = _ctx(tmp_path)
    ctx.agent._engine = SimpleNamespace()

    with pytest.raises(RuntimeError, match="Gateway boundary"):
        await BankRuntimeSandboxInstallHook().run(ctx)

    assert ctx.extras == {}
    assert ctx.agent.toolkit.tool_groups[0].tools == []
    assert sandbox_tools._STATE.get() is None
