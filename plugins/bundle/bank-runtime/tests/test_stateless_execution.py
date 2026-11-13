from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from bank_runtime.remote_state import (
    RemoteStateClient,
    RemoteStateError,
    canonical_hash,
    execution_from_request,
)
from bank_runtime.session import (
    ManagedSessionCleanupHook,
    ManagedSessionCommitHook,
    ManagedSessionPrepareHook,
    current_managed_session_scope,
)
from qwenpaw.hooks.session.session_hook import SessionLoadHook


def request(**changes):
    package = {"revision": 4, "format": "agent-state-v1", "agent_state": {
        "state": {"session_id": "session-a", "summary": "已核实12条记录", "context": [
            {"id": "a", "name": "assistant", "role": "assistant", "content": [
                {"type": "tool_use", "id": "call-a", "name": "bank_read", "input": {"key": "value"}}]},
            {"id": "b", "name": "bank_read", "role": "tool", "content": [
                {"type": "tool_result", "id": "call-a", "name": "bank_read", "output": [
                    {"type": "text", "text": "12条记录"}]}]},
        ]},
        "scroll": {"persisted_ids": ["a"], "seq_by_id": {"a": [99, 99]}},
    }}
    package["hash"] = canonical_hash(package)
    values = dict(channel="bank-runtime", runtime_task_id="task-a", session_id="session-a",
        user_id="user-a", session_contract_version="2.0", qwenpaw_session_state="active",
        session_operation="append", runtime_execution={"protocol": "bank-stateless-v1",
            "execution_id": "execution-a", "epoch": 2, "branch_id": "main", "base_revision": 4,
            "context_package": package},
        runtime_tool_gateway={"base_url": "https://runtime.test", "token": "test-token",
            "worker_agent_id": "agent-a", "task_id": "task-a", "session_id": "session-a",
            "policy_snapshot_id": "policy-a", "tool_session_id": "tool-session-a"})
    values.update(changes)
    return SimpleNamespace(**values)


def test_recovery_generation_survives_callback_and_changes_execution_identity():
    first = request()
    first.runtime_execution['recovery_generation'] = 'a'*32
    old = execution_from_request(first,'agent-a')
    assert old.scope_payload()['recovery_generation'] == 'a'*32
    first.runtime_execution['recovery_generation'] = 'b'*32
    assert execution_from_request(first,'agent-a').run_key != old.run_key
    for invalid in ('invalid',None,42):
        first.runtime_execution['recovery_generation'] = invalid
        with pytest.raises(RemoteStateError):execution_from_request(first,'agent-a')


class NoLocalSession:
    async def get_session_state_dict(self, **kwargs):
        pytest.fail("remote execution read a local session")

    async def save_session_state(self, **kwargs):
        pytest.fail("remote execution wrote a local session")


def context(req=None, state=None):
    state = state or {"state": {"context": [
        {"role": "assistant", "content": [{"type": "text", "text": "已完成核对"},
            {"type": "thinking", "thinking": "private-reasoning"}]}]}}
    return SimpleNamespace(request=req or request(), workspace=SimpleNamespace(session=NoLocalSession()),
        agent_id="agent-a", session_id="session-a", extras={}, mode_state={}, session_state=None,
        agent=SimpleNamespace(state_dict=lambda: copy.deepcopy(state)), error=None)


@pytest.mark.asyncio
async def test_remote_session_restores_on_empty_worker_and_stages_without_local_commit(monkeypatch):
    sent = []
    async def post(self, body):
        sent.append(body)
        return {"candidate_id": body["candidate_id"], "state_hash": body["state_hash"], "status": "staged"}
    monkeypatch.setattr(RemoteStateClient, "_post", post)
    ctx = context()
    try:
        await ManagedSessionPrepareHook().run(ctx)
        await SessionLoadHook().run(ctx)
        restored = ctx.session_state
        assert restored["state"]["summary"] == "已核实12条记录"
        assert [m["role"] for m in restored["state"]["context"]] == ["assistant", "tool"]
        assert not restored.get("scroll", {}).get("seq_by_id")
        await ManagedSessionCommitHook().run(ctx)
        scope = current_managed_session_scope()
        assert scope is not None and not scope.committed
        assert scope.candidate_id == sent[0]["candidate_id"]
        assert sent[0]["operation"] == "stage"
        assert sent[0]["epoch"] == 2
        assert "private-reasoning" not in json.dumps(sent)
    finally:
        await ManagedSessionCleanupHook().run(ctx)


@pytest.mark.asyncio
async def test_stale_epoch_stage_failure_propagates_before_completion(monkeypatch):
    async def post(self, body):
        raise RemoteStateError("WORKER_EPOCH_STALE")
    monkeypatch.setattr(RemoteStateClient, "_post", post)
    ctx = context()
    try:
        await ManagedSessionPrepareHook().run(ctx)
        with pytest.raises(RemoteStateError, match="WORKER_EPOCH_STALE"):
            await ManagedSessionCommitHook().run(ctx)
        assert not current_managed_session_scope().committed
    finally:
        await ManagedSessionCleanupHook().run(ctx)


@pytest.mark.parametrize("mutation", ["hash", "epoch", "scope", "revision"])
def test_execution_rejects_tampering_before_any_local_fallback(mutation):
    req = request()
    if mutation == "hash":
        req.runtime_execution["context_package"]["agent_state"]["state"]["summary"] = "tampered"
    elif mutation == "epoch":
        req.runtime_execution["epoch"] = True
    elif mutation == "scope":
        req.runtime_tool_gateway["task_id"] = "another-task"
    else:
        req.runtime_execution["base_revision"] = 5
    with pytest.raises(RemoteStateError):
        execution_from_request(req, "agent-a")


def test_native_request_does_not_install_remote_execution():
    assert execution_from_request(request(channel="console"), "agent-a") is None
    assert execution_from_request(request(runtime_execution=None), "agent-a") is None


@pytest.mark.asyncio
async def test_result_round_trip_is_scoped_and_paged_without_file_path(monkeypatch):
    stored = {}
    sent = []
    async def post(self, body):
        sent.append(body)
        if body["operation"] == "put_result":
            stored[body["result_id"]] = body["content"]
            return {"result_ref": "runtime-result:" + body["result_id"], "content_hash": body["content_hash"]}
        data = stored[body["result_ref"].removeprefix("runtime-result:")]
        end = min(len(data), body["offset"] + body["limit"])
        return {"content": data[body["offset"]:end], "offset": body["offset"],
            "next_offset": end, "has_more": end < len(data)}
    monkeypatch.setattr(RemoteStateClient, "_post", post)
    client = RemoteStateClient(execution_from_request(request(), "agent-a"))
    ref = await client.put_result("tool_result", "结果中文\n下一行")
    page = await client.get_result(ref, offset=0, limit=4)
    assert page == {"content": "结果中文", "offset": 0, "next_offset": 4, "has_more": True}
    assert all(body["task_id"] == "task-a" and body["branch_id"] == "main" for body in sent)
    with pytest.raises(RemoteStateError):
        await client.get_result("/tmp/other-user.txt")
    with pytest.raises(RemoteStateError):
        await client.get_result(ref, limit=32769)


@pytest.mark.asyncio
async def test_worker_state_transport_sends_auth_and_rejects_stale_response(monkeypatch):
    captured = []
    def handle(req):
        captured.append(req)
        return httpx.Response(409, json={"detail": {"code": "WORKER_EPOCH_STALE", "message": "private server detail"}})
    original = httpx.AsyncClient
    monkeypatch.setattr("bank_runtime.remote_state.httpx.AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs))
    client = RemoteStateClient(execution_from_request(request(), "agent-a"))
    with pytest.raises(RemoteStateError, match="WORKER_EPOCH_STALE") as caught:
        await client.put_result("tool_result", "answer")
    assert "private server detail" not in str(caught.value)
    assert str(captured[0].url) == "https://runtime.test/runtime/v1/worker-state"
    assert captured[0].headers["Authorization"] == "Bearer test-token"
    assert captured[0].headers["X-Agent-Id"] == "agent-a"


@pytest.mark.asyncio
async def test_pruner_and_offloader_use_remote_results_without_local_files(tmp_path, monkeypatch):
    from agentscope.message import TextBlock, ToolResultState
    from agentscope.tool import ToolResponse
    from qwenpaw.config.config import AgentProfileConfig
    from qwenpaw.runtime.builder import AgentBuilder
    writes = []
    def post_sync(self, body):
        writes.append(body)
        return {"result_ref": "runtime-result:" + body["result_id"], "content_hash": body["content_hash"]}
    async def post(self, body):
        return post_sync(self, body)
    monkeypatch.setattr(RemoteStateClient, "_post_sync", post_sync)
    monkeypatch.setattr(RemoteStateClient, "_post", post)
    ctx = context()
    ctx.workspace.workspace_dir = str(tmp_path)
    cfg = AgentProfileConfig(id="agent-a", name="Test")
    cfg.running.light_context_config.tool_result_pruning_config.pruning_recent_msg_max_bytes = 100
    try:
        await ManagedSessionPrepareHook().run(ctx)
        offloader = AgentBuilder._build_offloader(ctx, cfg)
        middleware = AgentBuilder._build_tool_result_pruning_middleware(ctx, cfg)
        response = ToolResponse(id="tool-a", name="read_data", content=[TextBlock(text="内容\n" * 100)], state=ToolResultState.SUCCESS)
        response = await middleware.prune_tool_response_async(response)
        assert "read_runtime_result" in str(response.content)
        assert "read_file" not in str(response.content)
        assert writes[0]["content"] == "内容\n" * 100
        assert not list(tmp_path.rglob("*.txt"))
        assert offloader.client.execution.execution_id == "execution-a"
        temporary = Path(ctx.extras["request_context_storage"]["scroll_workspace_dir"])
        assert temporary.exists()
    finally:
        await ManagedSessionCleanupHook().run(ctx)
    assert not temporary.exists()


def test_remote_ingress_and_stop_do_not_access_chats_json(monkeypatch):
    from test_bank_runtime_ingress import _client, _workspace, _FakeChannel, _StopTracker
    from bank_runtime.remote_state import execution_run_key
    workspace = _workspace(channel=_FakeChannel())
    class NoChats:
        async def get_or_create_chat(self, *args, **kwargs):
            pytest.fail("remote ingress accessed chats.json")
        async def get_chat_id_by_session(self, *args, **kwargs):
            pytest.fail("remote stop accessed chats.json")
    workspace.chat_manager = NoChats()
    async def post(self, payload):
        if payload['operation'] == 'claim_producer':
            return {'producer': False, 'producer_id': 'other-producer', 'status': 'done', 'last_sequence': 1}
        return {'items': [{'sequence': 1, 'event': {'event': 'answer.completed', 'payload': {'text': 'ok'}}, 'terminal': True}],
            'status': 'done', 'last_sequence': 1}
    monkeypatch.setattr(RemoteStateClient, '_post', post)
    stopped = []
    monkeypatch.setattr('bank_runtime.execution_stream.stop_local_execution', lambda key: stopped.append(key) or True)
    body = vars(request())
    body["input"] = [{"role": "user", "content": [{"type": "text", "text": "检查"}]}]
    headers = {"Authorization": "Bearer candidate-secret", "X-Agent-Id": "agent-a"}
    client = _client(monkeypatch, workspace)
    response = client.post("/api/bank-runtime/agents/agent-a/chat", json=body, headers=headers)
    assert response.status_code == 200
    workspace.task_tracker = _StopTracker()
    stop = client.post("/api/bank-runtime/agents/agent-a/chat/stop?chat_id=session-a", json=body, headers=headers)
    assert stop.status_code == 200
    assert workspace.task_tracker.stopped == []
    assert stopped == [execution_run_key("agent-a", "task-a", "session-a", "user-a", body["runtime_execution"])]


@pytest.mark.asyncio
async def test_remote_gateway_never_writes_outbox_and_unconfirmed_result_blocks_stage(monkeypatch):
    from bank_runtime.gateway.client import GatewayClient, GatewayConfig, GatewayError
    cfg = GatewayConfig("https://runtime.test", "/runtime/v1/tool-calls", "token", "task-a",
        "session-a", "tool-session-a", "policy-a", "scope-a", "a" * 64, "2.0", "trace-a", "agent-a")
    client = GatewayClient(cfg, execution=execution_from_request(request(), "agent-a"))
    assert client.outbox is None and client.guard_outbox is None
    calls = []
    async def fail(body):
        calls.append(body)
        raise GatewayError("lost acknowledgement")
    monkeypatch.setattr(client, "_post", fail)
    with pytest.raises(GatewayError):
        await client.report_result("tool-a", "completed", 1)
    assert all(item["epoch"] == 2 and item["execution_id"] == "execution-a" for item in calls)
    assert client.pending_acknowledgements
    ctx = context()
    ctx.agent._acting_middlewares = [SimpleNamespace(client=client)]
    try:
        await ManagedSessionPrepareHook().run(ctx)
        with pytest.raises(RemoteStateError):
            await ManagedSessionCommitHook().run(ctx)
    finally:
        await ManagedSessionCleanupHook().run(ctx)


@pytest.mark.asyncio
async def test_result_reader_reaches_model_with_guard_and_rejects_replaced_tool(monkeypatch):
    from agentscope.message import ToolCallBlock, SystemMsg, UserMsg
    from agentscope.permission import PermissionBehavior
    from agentscope.state import AgentState
    from agentscope.tool import Toolkit, FunctionTool
    from test_personalization import _request
    from test_personal_skill_boundary import _Guard
    from bank_runtime.personalization import BankRuntimePersonalizationHook, BankRuntimePersonalizationCleanupHook
    from bank_runtime.gateway.middleware import BankRuntimeGatewayMiddleware, BankRuntimeGatewayInstallHook
    from bank_runtime.gateway.hooks import BankRuntimeToolVisibilityHook
    monkeypatch.setattr("qwenpaw.config.config.load_agent_config", lambda _: SimpleNamespace(approval_level="auto"))
    req = request(identity_json=_request().identity_json,
        runtime_tool_visibility={"authoritative": True, "worker_type": "qwenpaw", "binding_snapshot_hash": "sha256:" + "a" * 64, "worker_tool_names": []})
    req.runtime_tool_gateway["capability_snapshot_hash"] = "a" * 64
    ctx = context(req)
    class NoBusinessCalls:
        config = SimpleNamespace(agent_id="agent-a", task_id="task-a", session_id="session-a")
        async def preflight(self, *args, **kwargs):
            pytest.fail("result reader acquired a business grant")
    middleware = BankRuntimeGatewayMiddleware(NoBusinessCalls())
    ctx.agent = SimpleNamespace(toolkit=Toolkit(), state=AgentState(), _system_prompt="BASE",
        _acting_middlewares=[middleware], _engine=_Guard())
    ctx.input_msgs = []
    calls = []
    async def post(self, body):
        calls.append(body)
        if body['operation'] == 'poll_inputs':
            from bank_runtime.remote_state import EMPTY_INPUT_HASH
            return {'revision': 0, 'content_hash': EMPTY_INPUT_HASH, 'messages': []}
        return {"content": "受控结果", "offset": 0, "next_offset": 4, "has_more": False}
    monkeypatch.setattr(RemoteStateClient, "_post", post)
    try:
        await ManagedSessionPrepareHook().run(ctx)
        await BankRuntimePersonalizationHook().run(ctx)
        await BankRuntimeGatewayInstallHook().run(ctx)
        await BankRuntimeToolVisibilityHook().run(ctx)
        schemas = await ctx.agent.toolkit.get_tool_schemas()
        captured = {}
        async def model(**kwargs):
            captured.update(kwargs)
        await middleware.on_model_call(ctx.agent, {"messages": [SystemMsg("system", "BASE"), UserMsg("user", "读取结果")], "tools": schemas}, model)
        assert {schema["function"]["name"] for schema in captured["tools"]} == {
            "read_runtime_result", "delegate_runtime_expert", "read_runtime_expert_results"}
        reader = middleware.result_recall
        payload = {"result_ref": "runtime-result:result_" + "a" * 64}
        assert (await ctx.agent._engine.check_permission(reader.tool, payload)).behavior == PermissionBehavior.ALLOW
        call = ToolCallBlock(id="read-a", name="read_runtime_result", input=json.dumps(payload))
        async def execute():
            async for item in ctx.agent.toolkit.call_tool(call, ctx.agent.state):
                yield item
        result = [item async for item in middleware.on_acting(ctx.agent, {"tool_call": call}, execute)]
        assert "受控结果" in str(result)
        assert calls[0]["branch_id"] == "main"
        ctx.agent._engine.delegate.behavior = PermissionBehavior.DENY
        assert (await ctx.agent._engine.check_permission(reader.tool, payload)).behavior == PermissionBehavior.DENY
        async def forged():
            return None
        replacement = FunctionTool(forged, name="read_runtime_result", is_read_only=True)
        assert not reader.recognizes(replacement)
        for group in ctx.agent.toolkit.tool_groups:
            group.tools[:] = [replacement if item is reader.tool else item for item in group.tools]
        assert not await reader.visible()
    finally:
        await BankRuntimePersonalizationCleanupHook().run(ctx)
        await ManagedSessionCleanupHook().run(ctx)
