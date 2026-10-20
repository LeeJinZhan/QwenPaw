"""Trusted caller context travels through the admitted tool path, never arguments."""
from __future__ import annotations

import asyncio
import copy
import json
import sys
from contextlib import aclosing
from pathlib import Path
from types import SimpleNamespace

import pytest
from agentscope.message import ToolCallBlock, ToolResultState
from agentscope.permission import PermissionBehavior
from agentscope.tool import ToolResponse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bank_runtime.gateway.client import GatewayError
from bank_runtime.gateway.middleware import GatewayPermissionEngine, bank_runtime_middleware_factory
from bank_runtime.sandbox.executor import RuntimeSandboxExecutor
from qwenpaw.drivers.mcp_context import current_mcp_metadata, mcp_call_metadata
from test_gateway_middleware import _Client, _DelegateEngine


def make_middleware(monkeypatch, user="user-a", *, omit=(), **changes):
    monkeypatch.setattr(RuntimeSandboxExecutor, "from_request", lambda **kwargs: None)
    identity = {
        "user_id": user, "display_name": "测试用户", "org_id": "org-" + user,
        "org_name": "测试机构", "roles": ["normal_user"], "role_codes": ["normal_user"],
        "role_names": ["客户经理", "业务主管"], "allowed_customer_ids": [],
        "assistant_code": "assistant-" + user, "assistant_name": "业务助手",
        "scenario_code": "assistant-" + user,
    }
    identity.update(changes)
    for key in omit:
        identity.pop(key, None)
    gateway = {
        "protocol": "preflight_guard_result_v2", "base_url": "http://127.0.0.1:8765",
        "endpoint": "/runtime/v1/tool-calls", "token": "test-only", "task_id": "task-" + user,
        "session_id": "session-" + user, "tool_session_id": "wts-" + user,
        "policy_snapshot_id": "policy", "task_scope_id": "scope-" + user,
        "capability_snapshot_hash": "sha256:capability", "worker_protocol_version": "runtime-worker/v1",
        "trace_id": "trace", "worker_agent_id": "bank-assistant",
    }
    request = SimpleNamespace(channel="bank-runtime", user_id=user, identity_json=identity,
        runtime_tool_gateway=gateway, request_context={"runtime_tool_gateway": gateway})
    middleware = bank_runtime_middleware_factory(
        SimpleNamespace(request=request, agent_id="bank-assistant"), SimpleNamespace())
    assert not middleware.configuration_error
    client = _Client()
    client.config = SimpleNamespace(task_id="task-" + user)
    middleware.client = client
    return middleware, identity


async def admit(middleware, name="Business__search", arguments=None, behavior=PermissionBehavior.ALLOW):
    arguments = arguments or {"keyword": "制度"}
    engine = GatewayPermissionEngine(_DelegateEngine(behavior, middleware.client.events), middleware)
    tool = SimpleNamespace(name=name, is_external_tool=False)
    decision = await engine.check_permission(tool, arguments)
    return decision, ToolCallBlock(id="model-call", name=name, input=json.dumps(arguments))


@pytest.mark.asyncio
async def test_admitted_call_receives_snapshot_and_preserves_other_metadata(monkeypatch):
    middleware, source = make_middleware(monkeypatch)
    _, call = await admit(middleware)
    source["role_names"].append("伪造的后续修改")
    source["assistant_name"] = "后续修改"
    seen = []

    async def execute():
        seen.append(copy.deepcopy(current_mcp_metadata()))
        assert json.loads(call.input) == {"keyword": "制度"}
        yield ToolResponse(id=call.id, content=[], state=ToolResultState.SUCCESS)

    outer = {"progressToken": "progress-1", "bank_runtime_context": {"user_id": "forged"}}
    with mcp_call_metadata(outer):
        async for _ in middleware.on_acting(None, {"tool_call": call}, execute):
            assert current_mcp_metadata() == outer
    context = seen[0]["bank_runtime_context"]
    assert context == {
        "user_id": "user-a", "display_name": "测试用户", "org_id": "org-user-a",
        "org_name": "测试机构", "role_codes": ["normal_user"],
        "role_names": ["客户经理", "业务主管"],
        "assistant_code": "assistant-user-a", "assistant_name": "业务助手",
    }
    assert seen[0]["progressToken"] == "progress-1"
    assert current_mcp_metadata() is None


@pytest.mark.asyncio
async def test_parallel_users_and_assistants_do_not_share_context(monkeypatch):
    instances = [make_middleware(monkeypatch, user)[0] for user in ("a", "b")]
    barrier = asyncio.Barrier(2)

    async def run(middleware, user):
        _, call = await admit(middleware)
        async def execute():
            await barrier.wait()
            assert current_mcp_metadata()["bank_runtime_context"]["user_id"] == user
            assert current_mcp_metadata()["bank_runtime_context"]["assistant_code"] == "assistant-" + user
            yield ToolResponse(id=call.id, content=[], state=ToolResultState.SUCCESS)
        async for _ in middleware.on_acting(None, {"tool_call": call}, execute):
            assert current_mcp_metadata() is None
    await asyncio.gather(*(run(m, u) for m, u in zip(instances, ("a", "b"))))


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
async def test_exception_and_cancellation_reset_metadata(monkeypatch, failure):
    middleware, _ = make_middleware(monkeypatch)
    _, call = await admit(middleware)
    async def execute():
        assert current_mcp_metadata()["bank_runtime_context"]["user_id"] == "user-a"
        raise failure("test interruption")
        yield
    with pytest.raises(asyncio.CancelledError if failure is asyncio.CancelledError else GatewayError):
        async for _ in middleware.on_acting(None, {"tool_call": call}, execute):
            pass
    assert current_mcp_metadata() is None


@pytest.mark.asyncio
async def test_generator_closed_in_another_task_does_not_leak_context(monkeypatch):
    middleware, _ = make_middleware(monkeypatch)
    _, call = await admit(middleware)
    closed = []
    async def execute():
        try:
            yield ToolResponse(id=call.id, content=[], state=ToolResultState.SUCCESS)
        finally:
            closed.append(current_mcp_metadata()["bank_runtime_context"]["user_id"])
    stream = middleware.on_acting(None, {"tool_call": call}, execute)
    await anext(stream)
    assert current_mcp_metadata() is None
    await asyncio.create_task(stream.aclose())
    assert closed == ["user-a"]
    assert current_mcp_metadata() is None


@pytest.mark.asyncio
async def test_document_grant_and_caller_context_coexist(monkeypatch):
    from bank_runtime.gateway.document_access import consume_document_call, current_document_task
    middleware, _ = make_middleware(monkeypatch)
    # Exercise the common dispatch wrapper after authorization; the existing
    # file-preparation suite covers acquiring a valid document operation permit.
    arguments = {"files": []}
    async def execute():
        meta = current_mcp_metadata()
        assert meta["bank_runtime_context"]["user_id"] == "user-a"
        with consume_document_call(meta, "parse_documents", arguments):
            assert current_document_task() == "task-user-a"
        yield "done"
    async with aclosing(middleware._authorized_native_call(
        "MinerU__parse_documents", arguments, execute)) as stream:
        assert [item async for item in stream] == ["done"]
    assert current_mcp_metadata() is None


@pytest.mark.asyncio
async def test_denied_tool_never_reaches_execution(monkeypatch):
    middleware, _ = make_middleware(monkeypatch)
    decision, call = await admit(middleware, behavior=PermissionBehavior.DENY)
    assert decision.behavior == PermissionBehavior.DENY
    async def execute():
        pytest.fail("Denied tool executed")
        yield
    with pytest.raises(GatewayError):
        async for _ in middleware.on_acting(None, {"tool_call": call}, execute):
            pass
    assert current_mcp_metadata() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"user_id": "forged"}, {"role_names": "管理员"}, {"role_names": [False]},
    {"role_names": ["x"] * 65}, {"role_names": ["x\n"]},
    {"assistant_name": {}}, {"org_name": "x" * 257},
    {"roles": ["admin"]}, {"allowed_customer_ids": ["customer-1"]},
])
async def test_invalid_identity_never_reaches_mcp(monkeypatch, changes):
    middleware, _ = make_middleware(monkeypatch, **changes)
    _, call = await admit(middleware)
    async def execute():
        pytest.fail("Invalid identity reached MCP")
        yield
    with pytest.raises(GatewayError):
        async for _ in middleware.on_acting(None, {"tool_call": call}, execute):
            pass


@pytest.mark.asyncio
async def test_old_optional_fields_have_empty_defaults(monkeypatch):
    middleware, _ = make_middleware(monkeypatch, omit=("org_name", "role_names", "assistant_code", "assistant_name"))
    _, call = await admit(middleware)
    async def execute():
        context = current_mcp_metadata()["bank_runtime_context"]
        assert context["org_name"] == ""
        assert context["role_names"] == []
        assert context["assistant_code"] == "assistant-user-a"
        assert context["assistant_name"] == ""
        yield ToolResponse(id=call.id, content=[], state=ToolResultState.SUCCESS)
    async for _ in middleware.on_acting(None, {"tool_call": call}, execute):
        pass


@pytest.mark.asyncio
async def test_missing_required_identity_cannot_fall_back_to_outer_metadata(monkeypatch):
    middleware, _ = make_middleware(monkeypatch, omit=("user_id", "roles"))
    _, call = await admit(middleware)
    async def execute():
        pytest.fail("Missing identity reached MCP")
        yield
    with mcp_call_metadata({"bank_runtime_context": {"user_id": "forged"}}):
        with pytest.raises(GatewayError):
            async for _ in middleware.on_acting(None, {"tool_call": call}, execute):
                pass


def test_non_bank_requests_do_not_install_metadata(monkeypatch):
    request = SimpleNamespace(channel="console", identity_json={"user_id": "forged"})
    assert bank_runtime_middleware_factory(SimpleNamespace(request=request), None) is None
    assert current_mcp_metadata() is None
