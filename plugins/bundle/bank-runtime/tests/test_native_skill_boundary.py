from pathlib import Path
import json
import sys
from types import SimpleNamespace

import pytest
from agentscope.message import ToolCallBlock
from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.skill import Skill
from agentscope.state import AgentState
from agentscope.tool import Toolkit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bank_runtime.gateway.middleware import BankRuntimeGatewayMiddleware, GatewayPermissionEngine


class DenyingClient:
    async def preflight(self, *args, **kwargs):
        raise RuntimeError("not authorized")


class Guard:
    async def check_permission(self, *args):
        return PermissionDecision(behavior=PermissionBehavior.ALLOW, message="Allowed")


def agent_fixture():
    toolkit = Toolkit(tools=[], skills_or_loaders=[Skill(
        name="browser", description="Browser guidance", dir="/unused",
        markdown="Read instructions; network access still requires authorization.", updated_at=0,
    )])
    return SimpleNamespace(toolkit=toolkit, state=AgentState())


@pytest.mark.asyncio
async def test_native_read_is_scoped_and_one_use_without_granting_browser():
    agent = agent_fixture()
    middleware = BankRuntimeGatewayMiddleware(DenyingClient())
    middleware.bind_native_skills(agent)
    engine = GatewayPermissionEngine(Guard(), middleware)
    viewer = agent.toolkit.builtin_skill_viewer.tool
    decision = await engine.check_permission(viewer, {"skill": "browser"})
    assert decision.behavior == PermissionBehavior.ALLOW
    call = ToolCallBlock(id="read1", name="Skill", input=json.dumps({"skill": "browser"}))

    async def execute():
        async for item in agent.toolkit.call_tool(call, agent.state):
            yield item

    result = [item async for item in middleware.on_acting(agent, {"tool_call": call}, execute)]
    assert "network access" in result[-1].content[0].text
    with pytest.raises(Exception) as denied:
        _ = [item async for item in middleware.on_acting(agent, {"tool_call": call}, execute)]
    assert "permit" in str(denied.value.__cause__)
    assert "Gateway" not in str(denied.value)
    for tool, payload in [
        (SimpleNamespace(name="Skill"), {"skill": "browser"}),
        (SimpleNamespace(name="browser"), {"code": "navigate()"}),
        (viewer, {"skill": "../other/SKILL.md"}),
        (viewer, {"skill": "browser", "path": "/etc/passwd"}),
    ]:
        assert (await engine.check_permission(tool, payload)).behavior == PermissionBehavior.DENY


@pytest.mark.asyncio
async def test_model_schema_filters_dynamic_tools_but_preserves_native_skill():
    agent = agent_fixture()
    middleware = BankRuntimeGatewayMiddleware(DenyingClient())
    middleware.bind_native_skills(agent)
    middleware.allowed_tool_names = frozenset({"approved"})
    schemas = [{"type": "function", "function": {"name": name}} for name in
               ["approved", "browser", "Skill", "ResetTools", "UnapprovedMCP__read"]]

    async def model(**kwargs):
        return kwargs["tools"]

    actual = await middleware.on_model_call(agent, {"tools": schemas}, model)
    assert [s["function"]["name"] for s in actual] == ["approved", "Skill"]


@pytest.mark.asyncio
async def test_reader_cannot_move_between_agents_or_replace_viewer_after_admission():
    agent = agent_fixture()
    other = agent_fixture()
    middleware = BankRuntimeGatewayMiddleware(DenyingClient())
    middleware.bind_native_skills(agent)
    engine = GatewayPermissionEngine(Guard(), middleware)
    payload = {"skill": "browser"}
    assert (await engine.check_permission(other.toolkit.builtin_skill_viewer.tool, payload)).behavior == PermissionBehavior.DENY
    assert (await engine.check_permission(agent.toolkit.builtin_skill_viewer.tool, payload)).behavior == PermissionBehavior.ALLOW
    agent.toolkit.builtin_skill_viewer = other.toolkit.builtin_skill_viewer
    call = ToolCallBlock(id="swapped", name="Skill", input=json.dumps(payload))

    async def forbidden():
        raise AssertionError("replaced viewer must not execute")
        yield

    with pytest.raises(Exception) as rejected:
        _ = [item async for item in middleware.on_acting(agent, {"tool_call": call}, forbidden)]
    assert "scope changed" in str(rejected.value.__cause__)


@pytest.mark.asyncio
async def test_native_read_respects_local_guard_and_missing_managed_context():
    agent = agent_fixture()
    middleware = BankRuntimeGatewayMiddleware(DenyingClient())
    middleware.bind_native_skills(agent)

    class BlockGuard:
        async def check_permission(self, *args):
            return PermissionDecision(behavior=PermissionBehavior.DENY, message="internal rule")

    engine = GatewayPermissionEngine(BlockGuard(), middleware)
    assert (await engine.check_permission(agent.toolkit.builtin_skill_viewer.tool, {"skill": "browser"})).behavior == PermissionBehavior.DENY
    middleware.client = None
    engine = GatewayPermissionEngine(Guard(), middleware)
    assert (await engine.check_permission(agent.toolkit.builtin_skill_viewer.tool, {"skill": "browser"})).behavior == PermissionBehavior.DENY
