from __future__ import annotations

import asyncio
import copy
import json
import sys
from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from agentscope.agent import Agent
from agentscope.message import Msg, TextBlock, ToolCallBlock
from agentscope.model import ChatResponse
from agentscope.state import AgentState
from agentscope.tool import Toolkit

from bank_runtime.channel import BankRuntimeChannel
from bank_runtime.events import project_sse_stream
from bank_runtime.gateway.delegations import DelegationClient
from bank_runtime.gateway.middleware import BankRuntimeGatewayMiddleware, BankRuntimeGatewayInstallHook
from bank_runtime.gateway.hooks import BankRuntimeToolVisibilityHook
from bank_runtime.personalization import BankRuntimePersonalizationHook, BankRuntimePersonalizationCleanupHook
from bank_runtime.remote_state import RemoteStateClient, RemoteStateError, canonical_hash, execution_from_request, watch_execution
from bank_runtime.session import ManagedSessionPrepareHook, ManagedSessionCommitHook, ManagedSessionCleanupHook, ManagedSessionErrorHook
from qwenpaw.hooks.session.session_hook import SessionLoadHook, SessionSaveHook
from qwenpaw.runtime.hooks import HookRegistry
from qwenpaw.runtime.runtime import Runtime
from qwenpaw.schemas import AgentRequest
from test_stateless_execution import request, NoLocalSession
from test_personalization import _request as identity_request
from test_personal_skill_boundary import _Guard


@pytest.mark.asyncio
async def test_real_agent_loop_checkpoints_after_pending_tool_result_then_releases_worker(monkeypatch):
    calls = []
    async def post(self, body):
        calls.append(body)
        if body['operation'] == 'poll_inputs':
            from bank_runtime.remote_state import EMPTY_INPUT_HASH
            return {'revision': 0, 'content_hash': EMPTY_INPUT_HASH, 'messages': []}
        if body["operation"] == "delegate":
            return {"child_task_id": "child-a", "status": "queued", "branch_id": "branch-a",
                "role": {"role_id": "expert-a", "version": 1, "name": "Expert A"}}
        if body["operation"] == "results":
            if body['epoch'] == 3:
                return {"items": [{"child_task_id": "child-a", "role_id": "expert-a", "version": 1,
                    "status": "completed", "result": {"text": "专家已完成核对", "refs": []}}]}
            return {"items": [{"child_task_id": "child-a", "role_id": "expert-a", "version": 1, "status": "running"}]}
        if body["operation"] == "checkpoint":
            return {"checkpoint_id": body["candidate_id"].replace("candidate_", "checkpoint_"), "state_hash": body["state_hash"], "status": "checkpointed"}
        if body["operation"] == "status":
            return {"status": "active", "epoch": body['epoch']}
        if body["operation"] == "stage":
            return {"status": "staged", "candidate_id": body["candidate_id"], "state_hash": body["state_hash"]}
        pytest.fail("Unexpected state operation: " + body["operation"])
    monkeypatch.setattr(RemoteStateClient, "_post", post)
    monkeypatch.setattr("qwenpaw.config.config.load_agent_config", lambda _: SimpleNamespace(approval_level="auto"))
    class Model:
        model = "synthetic"
        context_size = 128000
        calls = 0
        async def count_tokens(self, *args, **kwargs):
            return 100
        async def __call__(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                block = ToolCallBlock(id="delegate-a", name="delegate_runtime_expert",
                    input=json.dumps({"role_id": "expert-a", "instruction": "核对内容", "purpose": "review"}))
            elif self.calls == 2:
                block = ToolCallBlock(id="results-a", name="read_runtime_expert_results",
                    input=json.dumps({"child_task_ids": ["child-a"]}))
            else:
                pytest.fail("Coordinator busy-polled instead of suspending")
            return ChatResponse(content=[block], is_last=True)
    class Client:
        config = SimpleNamespace(agent_id="agent-a", task_id="task-a", session_id="session-a")
        async def preflight(self, *args, **kwargs):
            pytest.fail("Orchestration incorrectly acquired a business grant")
    model = Model()
    middleware = BankRuntimeGatewayMiddleware(Client())
    agent = Agent(name="coordinator", system_prompt="BASE", model=model, toolkit=Toolkit(), middlewares=[middleware])
    agent._engine = _Guard()
    agent.state_dict = lambda: {"state": agent.state.model_dump(mode="json")}
    agent.load_state_dict = lambda state: setattr(agent, "state", AgentState.model_validate(state["state"]))
    registry = HookRegistry()
    for hook in (ManagedSessionPrepareHook(), SessionLoadHook(), BankRuntimePersonalizationHook(),
        BankRuntimeGatewayInstallHook(), BankRuntimeToolVisibilityHook(), ManagedSessionCommitHook(), SessionSaveHook(),
        ManagedSessionErrorHook(), BankRuntimePersonalizationCleanupHook(), ManagedSessionCleanupHook()):
        registry.register(hook)
    async def no_command(*args):
        return None
    workspace = SimpleNamespace(agent_id="agent-a", session=NoLocalSession(),
        plugins=SimpleNamespace(hook_registry=registry, modes=[], slash_command_registry=SimpleNamespace(dispatch=no_command)))
    runtime = Runtime(workspace=workspace, app_services=None)
    async def build(self, ctx):
        if ctx.session_state:
            agent.load_state_dict(ctx.session_state)
        return agent
    monkeypatch.setattr("qwenpaw.runtime.builder.AgentBuilder.build", build)
    req = request(identity_json=identity_request().identity_json,
        runtime_tool_visibility={"authoritative": True, "worker_type": "qwenpaw", "binding_snapshot_hash": "sha256:" + "a" * 64, "worker_tool_names": []})
    req.runtime_tool_gateway["capability_snapshot_hash"] = "a" * 64
    package = {"revision": 4, "format": "agent-state-v1", "agent_state": {"state": {"context": []}}}
    package["hash"] = canonical_hash(package)
    req.runtime_execution["context_package"] = package
    body = vars(req)
    body["input"] = [{"role": "user", "content": [{"type": "text", "text": "请专家核对"}]}]
    channel = BankRuntimeChannel.from_config(process=runtime.run, config=SimpleNamespace(enabled=True, bot_prefix="", media_dir=""))
    output = [json.loads(item.removeprefix("data: ")) async for item in project_sse_stream(channel.stream_one(AgentRequest(**body)), "task-a")]
    assert model.calls == 2
    assert output[-1]["event"] == "execution.waiting"
    assert not any(item.get("event") in {"answer.completed", "answer.failed"} for item in output)
    checkpoint = next(item for item in calls if item["operation"] == "checkpoint")
    context = checkpoint["agent_state"]["state"]["context"]
    assert context[0]["role"] == "user"
    # AgentScope 2 keeps tool call/result blocks in the same assistant Msg.
    protocol = [block for message in context for block in message.get("content", [])
        if block.get("type") in {"tool_call", "tool_result"}]
    assert [(block["type"], block["id"]) for block in protocol] == [
        ("tool_call", "delegate-a"), ("tool_result", "delegate-a"),
        ("tool_call", "results-a"), ("tool_result", "results-a")]
    assert "running" in json.dumps(protocol[-1])
    assert checkpoint["waiting_child_task_ids"] == ["child-a"]
    assert next(item for item in calls if item["operation"] == "delegate")["purpose"] == "review"

    class ResumedModel(Model):
        calls = 0
        async def __call__(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return ChatResponse(content=[ToolCallBlock(id='resumed-results', name='read_runtime_expert_results',
                    input=json.dumps({'child_task_ids': ['child-a']}))], is_last=True)
            assert self.calls == 2
            return ChatResponse(content=[TextBlock(text='专家核对完成。')], is_last=True)

    resumed_model = ResumedModel()
    resumed_middleware = BankRuntimeGatewayMiddleware(Client())
    resumed_agent = Agent(name='coordinator', system_prompt='BASE', model=resumed_model, toolkit=Toolkit(), middlewares=[resumed_middleware])
    resumed_agent._engine = _Guard()
    resumed_agent.state_dict = lambda: {'state': resumed_agent.state.model_dump(mode='json')}
    resumed_agent.load_state_dict = lambda state: setattr(resumed_agent, 'state', AgentState.model_validate(state['state']))
    async def rebuild(self, ctx):
        resumed_agent.load_state_dict(ctx.session_state)
        return resumed_agent
    monkeypatch.setattr('qwenpaw.runtime.builder.AgentBuilder.build', rebuild)
    body = copy.deepcopy(body)
    body['input'] = []
    body['runtime_execution'].update(epoch=3, resume=True)
    package = {'revision': 4, 'format': 'agent-state-v1', 'agent_state': checkpoint['agent_state'], 'confirmed_message_ids': []}
    package['hash'] = canonical_hash(package)
    body['runtime_execution']['context_package'] = package
    completed = [json.loads(item.removeprefix('data: ')) async for item in project_sse_stream(channel.stream_one(AgentRequest(**body)), 'task-a')]
    assert completed[-1]['event'] == 'answer.completed'
    staged = next(item for item in calls if item['operation'] == 'stage')
    resumed_context = staged['agent_state']['state']['context']
    assert sum(msg['role'] == 'user' for msg in resumed_context) == 1
    assert '专家核对完成' in json.dumps(resumed_context, ensure_ascii=False)
    assert 'resumed-results' in json.dumps(resumed_context)
    assert sum(item['operation'] == 'delegate' for item in calls) == 1


@pytest.mark.asyncio
async def test_watchdog_revocation_cancels_only_its_execution(monkeypatch):
    client = RemoteStateClient(execution_from_request(request(), "agent-a"))
    async def rejected(body):
        raise RemoteStateError("WORKER_EPOCH_STALE", http_status=409)
    monkeypatch.setattr(client, "_post", rejected)
    owner = asyncio.create_task(asyncio.sleep(60))
    unrelated = asyncio.create_task(asyncio.sleep(60))
    try:
        await watch_execution(client, owner, lambda: True, interval=0.001)
        with pytest.raises(asyncio.CancelledError):
            await owner
        assert not unrelated.cancelled() and not unrelated.done()
    finally:
        unrelated.cancel()
        await asyncio.gather(unrelated, return_exceptions=True)


@pytest.mark.asyncio
async def test_watchdog_network_loss_is_bounded_without_local_authority(monkeypatch):
    execution = replace(execution_from_request(request(), "agent-a"), lease_budget_seconds=0.02)
    client = RemoteStateClient(execution)
    async def unavailable(body):
        raise RemoteStateError()
    monkeypatch.setattr(client, "_post", unavailable)
    owner = asyncio.create_task(asyncio.sleep(60))
    await asyncio.wait_for(watch_execution(client, owner, lambda: True, interval=0.001), timeout=0.5)
    with pytest.raises(asyncio.CancelledError):
        await owner
