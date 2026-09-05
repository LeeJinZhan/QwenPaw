from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
from agentscope.message import ToolCallBlock, ToolResultState
from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.tool import ToolChunk, ToolResponse

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from bank_runtime.gateway.client import GatewayConfig, GatewayError
from bank_runtime.gateway.middleware import (
    BankRuntimeGatewayInstallHook,
    BankRuntimeGatewayMiddleware,
    GatewayPermissionEngine,
    bank_runtime_middleware_factory,
)
from qwenpaw.tool_calls import ToolCoordinator, ToolCoordinatorMiddleware


class _Client:
    def __init__(self) -> None:
        self.events: list[tuple] = []

    async def preflight(self, tool_name, tool_input, *, call_id):
        self.events.append(("preflight", tool_name, dict(tool_input), call_id))
        return {
            "tool_call_id": "runtime_call_001",
            "permit": {"payload": {"permit_id": "permit_001"}},
            "call_id": call_id,
        }

    async def report_guard(self, preflight, decision):
        self.events.append(("guard", decision, preflight["tool_call_id"]))
        return {"status": "executing" if decision == "allow" else "cancelled"}

    async def report_result(self, tool_call_id, status, duration_ms, error_code=""):
        self.events.append(("result", tool_call_id, status, error_code))
        return {"status": status}

    async def execute_runtime_tool(self, preflight, tool_name, tool_input):
        self.events.append(
            (
                "runtime_execute",
                preflight["tool_call_id"],
                tool_name,
                dict(tool_input),
            )
        )
        return {
            "tool_call_id": preflight["tool_call_id"],
            "decision": "allow",
            "status": "success",
            "result": {
                "artifact_job_id": "artifact_job_001",
                "generated_file_ids": ["generated_file_001"],
            },
        }


class _DelegateEngine:
    def __init__(self, decision: PermissionBehavior, events: list[tuple]) -> None:
        self.decision = decision
        self.events = events
        self.context = object()

    async def check_permission(self, tool, tool_input):
        self.events.append(("tool_guard", tool.name, dict(tool_input)))
        return PermissionDecision(
            behavior=self.decision,
            message="local guard decision",
        )


class _SandboxExecutor:
    def __init__(self, events, result=None) -> None:
        self.events = events
        self.result = result or {"exit_code": 0, "stdout": "sandbox-ok", "stderr": ""}

    async def execute(self, *, tool_call_id, tool_name, tool_input):
        self.events.append(
            ("sandbox_execute", tool_call_id, tool_name, dict(tool_input))
        )
        return dict(self.result)


@pytest.mark.asyncio
async def test_gateway_order_is_preflight_guard_execute_result() -> None:
    client = _Client()
    middleware = BankRuntimeGatewayMiddleware(client)
    engine = GatewayPermissionEngine(
        _DelegateEngine(PermissionBehavior.ALLOW, client.events),
        middleware,
    )
    tool = SimpleNamespace(name="policy_search", is_external_tool=False)
    decision = await engine.check_permission(tool, {"query": "制度"})
    assert decision.behavior == PermissionBehavior.ALLOW

    async def execute(**_kwargs):
        client.events.append(("execute",))
        yield ToolChunk(content=[], state=ToolResultState.RUNNING)
        yield ToolResponse(
            id="model_call_001",
            content=[],
            state=ToolResultState.SUCCESS,
        )

    call = ToolCallBlock(
        id="model_call_001",
        name="policy_search",
        input=json.dumps({"query": "制度"}, ensure_ascii=False),
    )
    output = [
        item
        async for item in middleware.on_acting(
            SimpleNamespace(),
            {"tool_call": call},
            execute,
        )
    ]

    assert output[-1].state == ToolResultState.SUCCESS
    assert [event[0] for event in client.events] == [
        "preflight",
        "tool_guard",
        "guard",
        "execute",
        "result",
    ]
    with pytest.raises(GatewayError):
        _ = [
            item
            async for item in middleware.on_acting(
                SimpleNamespace(),
                {"tool_call": call},
                execute,
            )
        ]


@pytest.mark.asyncio
async def test_terminal_chunk_and_response_report_result_once() -> None:
    client = _Client()
    middleware = BankRuntimeGatewayMiddleware(client)
    engine = GatewayPermissionEngine(
        _DelegateEngine(PermissionBehavior.ALLOW, client.events),
        middleware,
    )
    tool = SimpleNamespace(name="policy_search", is_external_tool=False)
    decision = await engine.check_permission(tool, {"query": "制度"})
    assert decision.behavior == PermissionBehavior.ALLOW

    async def execute(**_kwargs):
        yield ToolChunk(content=[], state=ToolResultState.ERROR)
        yield ToolResponse(
            id="model_call_001",
            content=[],
            state=ToolResultState.ERROR,
        )

    call = ToolCallBlock(
        id="model_call_001",
        name="policy_search",
        input=json.dumps({"query": "制度"}, ensure_ascii=False),
    )
    _ = [
        item
        async for item in middleware.on_acting(
            SimpleNamespace(),
            {"tool_call": call},
            execute,
        )
    ]

    assert [event[0] for event in client.events].count("result") == 1


@pytest.mark.asyncio
async def test_physical_tool_executes_only_through_runtime_sandbox() -> None:
    client = _Client()
    middleware = BankRuntimeGatewayMiddleware(
        client,
        sandbox_executor=_SandboxExecutor(client.events),
    )
    engine = GatewayPermissionEngine(
        _DelegateEngine(PermissionBehavior.ALLOW, client.events),
        middleware,
    )
    tool = SimpleNamespace(name="execute_shell_command", is_external_tool=False)
    decision = await engine.check_permission(tool, {"command": "pwd"})
    assert decision.behavior == PermissionBehavior.ALLOW

    async def forbidden_local_execute(**_kwargs):
        raise AssertionError("local QwenPaw execution must not run")
        yield

    call = ToolCallBlock(
        id="model_call_shell",
        name="execute_shell_command",
        input=json.dumps({"command": "pwd"}),
    )
    output = [
        item
        async for item in middleware.on_acting(
            SimpleNamespace(),
            {"tool_call": call},
            forbidden_local_execute,
        )
    ]

    assert len(output) == 1
    assert isinstance(output[0], ToolResponse)
    assert output[0].state == ToolResultState.SUCCESS
    assert "sandbox-ok" in output[0].content[0].text
    assert [event[0] for event in client.events] == [
        "preflight",
        "tool_guard",
        "guard",
        "sandbox_execute",
        "result",
    ]


@pytest.mark.asyncio
async def test_artifact_tool_executes_in_runtime_and_never_calls_local_function() -> None:
    client = _Client()
    middleware = BankRuntimeGatewayMiddleware(client)
    engine = GatewayPermissionEngine(
        _DelegateEngine(PermissionBehavior.ALLOW, client.events),
        middleware,
    )
    tool_input = {
        "artifact_type": "docx",
        "title": "会议纪要",
        "content": {"sections": [{"heading": "结论", "paragraphs": ["通过"]}]},
    }
    decision = await engine.check_permission(
        SimpleNamespace(name="artifact_generate", is_external_tool=False),
        tool_input,
    )
    assert decision.behavior == PermissionBehavior.ALLOW

    async def forbidden_local_execute(**_kwargs):
        raise AssertionError("artifact tools must execute in Runtime")
        yield

    call = ToolCallBlock(
        id="model_artifact_001",
        name="artifact_generate",
        input=json.dumps(tool_input, ensure_ascii=False),
    )
    output = [
        item
        async for item in middleware.on_acting(
            SimpleNamespace(),
            {"tool_call": call},
            forbidden_local_execute,
        )
    ]

    assert len(output) == 1
    assert output[0].state == ToolResultState.SUCCESS
    assert "artifact_job_001" in output[0].content[0].text
    assert [event[0] for event in client.events] == [
        "preflight",
        "tool_guard",
        "guard",
        "runtime_execute",
    ]


@pytest.mark.asyncio
async def test_nonzero_physical_shell_result_is_reported_as_failed() -> None:
    client = _Client()
    middleware = BankRuntimeGatewayMiddleware(
        client,
        sandbox_executor=_SandboxExecutor(
            client.events,
            {"exit_code": 2, "stdout": "", "stderr": "denied"},
        ),
    )
    engine = GatewayPermissionEngine(
        _DelegateEngine(PermissionBehavior.ALLOW, client.events),
        middleware,
    )
    decision = await engine.check_permission(
        SimpleNamespace(name="execute_shell_command", is_external_tool=False),
        {"command": "false"},
    )
    assert decision.behavior == PermissionBehavior.ALLOW

    async def forbidden_local_execute(**_kwargs):
        raise AssertionError("local QwenPaw execution must not run")
        yield

    call = ToolCallBlock(
        id="model_call_shell_failed",
        name="execute_shell_command",
        input=json.dumps({"command": "false"}),
    )
    output = [
        item
        async for item in middleware.on_acting(
            SimpleNamespace(),
            {"tool_call": call},
            forbidden_local_execute,
        )
    ]

    assert output[0].state == ToolResultState.ERROR
    assert client.events[-1][0:3] == ("result", "runtime_call_001", "failed")


@pytest.mark.asyncio
async def test_sandbox_broker_tool_uses_standard_gateway_permit_chain() -> None:
    client = _Client()
    middleware = BankRuntimeGatewayMiddleware(client)
    engine = GatewayPermissionEngine(
        _DelegateEngine(PermissionBehavior.ALLOW, client.events),
        middleware,
    )
    trusted = SimpleNamespace(
        name="runtime_sandbox_files_search",
        is_external_tool=False,
    )

    decision = await engine.check_permission(trusted, {"query": "制度"})
    assert decision.behavior == PermissionBehavior.ALLOW

    async def execute(**_kwargs):
        client.events.append(("execute",))
        yield ToolResponse(
            id="model_sandbox_search",
            content=[],
            state=ToolResultState.SUCCESS,
        )

    call = ToolCallBlock(
        id="model_sandbox_search",
        name="runtime_sandbox_files_search",
        input=json.dumps({"query": "制度"}, ensure_ascii=False),
    )
    _ = [
        item
        async for item in middleware.on_acting(
            SimpleNamespace(),
            {"tool_call": call},
            execute,
        )
    ]
    assert [event[0] for event in client.events] == [
        "preflight",
        "tool_guard",
        "guard",
        "execute",
        "result",
    ]


@pytest.mark.asyncio
async def test_sandbox_broker_execution_requires_gateway_permit_claim() -> None:
    middleware = BankRuntimeGatewayMiddleware(_Client())

    async def forbidden(**_kwargs):
        raise AssertionError("unclaimed broker tool must not execute")
        yield

    call = ToolCallBlock(
        id="model_sandbox_search",
        name="runtime_sandbox_files_search",
        input=json.dumps({"query": "制度"}, ensure_ascii=False),
    )
    with pytest.raises(GatewayError):
        _ = [
            item
            async for item in middleware.on_acting(
                SimpleNamespace(),
                {"tool_call": call},
                forbidden,
            )
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["run_tool_batch", "external_browser"])
async def test_nested_or_external_execution_is_preflighted_then_blocked(
    tool_name,
) -> None:
    client = _Client()
    middleware = BankRuntimeGatewayMiddleware(client)
    engine = GatewayPermissionEngine(
        _DelegateEngine(PermissionBehavior.ALLOW, client.events),
        middleware,
    )
    tool = SimpleNamespace(
        name=tool_name,
        is_external_tool=tool_name == "external_browser",
    )
    decision = await engine.check_permission(tool, {"value": "x"})

    assert decision.behavior == PermissionBehavior.DENY
    assert [event[0] for event in client.events] == [
        "preflight",
        "tool_guard",
        "guard",
    ]
    assert client.events[-1][1] == "block"


def test_gateway_config_rejects_tool_visibility_and_arbitrary_endpoint() -> None:
    payload = {
        "protocol": "preflight_guard_result_v2",
        "base_url": "http://127.0.0.1:8765",
        "endpoint": "/runtime/v1/tool-calls",
        "token": "secret",
        "task_id": "task_001",
        "session_id": "session_001",
        "tool_session_id": "wts_001",
        "policy_snapshot_id": "policy_001",
        "task_scope_id": "scope_001",
        "capability_snapshot_hash": "sha256:capability",
        "worker_protocol_version": "runtime-worker/v1",
        "trace_id": "trace_001",
        "worker_agent_id": "bank-assistant",
    }
    assert GatewayConfig.from_mapping(payload).agent_id == "bank-assistant"
    with pytest.raises(GatewayError):
        GatewayConfig.from_mapping({**payload, "allowed_tools": ["shell"]})
    with pytest.raises(GatewayError):
        GatewayConfig.from_mapping({**payload, "endpoint": "https://evil.test/call"})


def test_factory_is_channel_scoped_and_missing_gateway_stays_fail_closed() -> None:
    ordinary = SimpleNamespace(request=SimpleNamespace(channel="console"))
    assert bank_runtime_middleware_factory(ordinary, SimpleNamespace()) is None

    bank = SimpleNamespace(
        request=SimpleNamespace(channel="bank-runtime", request_context={}),
        agent_id="bank-assistant",
    )
    middleware = bank_runtime_middleware_factory(bank, SimpleNamespace())
    assert isinstance(middleware, BankRuntimeGatewayMiddleware)
    assert middleware.configuration_error


@pytest.mark.asyncio
async def test_install_hook_rejects_missing_middleware_and_removes_bypass_tools() -> (
    None
):
    hook = BankRuntimeGatewayInstallHook()
    request = SimpleNamespace(channel="bank-runtime")
    agent = SimpleNamespace(
        _acting_middlewares=[],
        _engine=SimpleNamespace(),
        toolkit=SimpleNamespace(
            tool_groups=[
                SimpleNamespace(
                    tools=[
                        SimpleNamespace(name="policy_search", is_external_tool=False),
                        SimpleNamespace(name="run_tool_batch", is_external_tool=False),
                        SimpleNamespace(name="external_browser", is_external_tool=True),
                    ]
                )
            ]
        ),
    )
    ctx = SimpleNamespace(request=request, agent=agent)
    with pytest.raises(GatewayError):
        await hook.run(ctx)

    client = _Client()
    middleware = BankRuntimeGatewayMiddleware(client)
    agent._acting_middlewares = [middleware]
    await hook.run(ctx)
    assert [tool.name for tool in agent.toolkit.tool_groups[0].tools] == [
        "policy_search"
    ]
    assert isinstance(agent._engine, GatewayPermissionEngine)


def test_qwenpaw_source_has_no_unmanaged_nested_toolkit_call_sites() -> None:
    source_root = PLUGIN_ROOT.parents[2] / "src" / "qwenpaw"
    direct_call_sites = []
    for path in source_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "call_tool"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "toolkit"
            for node in ast.walk(tree)
        ):
            direct_call_sites.append(path.relative_to(source_root).as_posix())
    assert direct_call_sites == ["agents/tools/run_tool_batch.py"]
    assert "run_tool_batch" in _blocked_tool_names()


@pytest.mark.asyncio
async def test_builtin_plugin_mcp_and_mode_tools_share_the_same_chain() -> None:
    for tool_name in (
        "builtin_read",
        "bank_assistant",
        "mcp.policy.search",
        "allowed_mode_tool",
    ):
        client = _Client()
        middleware = BankRuntimeGatewayMiddleware(client)
        engine = GatewayPermissionEngine(
            _DelegateEngine(PermissionBehavior.ALLOW, client.events),
            middleware,
        )
        decision = await engine.check_permission(
            SimpleNamespace(name=tool_name, is_external_tool=False),
            {"value": tool_name},
        )
        assert decision.behavior == PermissionBehavior.ALLOW

        async def execute(**_kwargs):
            client.events.append(("execute",))
            yield ToolResponse(
                id=f"call_{tool_name}",
                content=[],
                state=ToolResultState.SUCCESS,
            )

        call = ToolCallBlock(
            id=f"call_{tool_name}",
            name=tool_name,
            input=json.dumps({"value": tool_name}),
        )
        _ = [
            item
            async for item in middleware.on_acting(
                SimpleNamespace(),
                {"tool_call": call},
                execute,
            )
        ]
        assert [event[0] for event in client.events] == [
            "preflight",
            "tool_guard",
            "guard",
            "execute",
            "result",
        ]


@pytest.mark.asyncio
async def test_tool_coordinator_background_execution_stays_inside_gateway() -> None:
    client = _Client()
    gateway = BankRuntimeGatewayMiddleware(client)
    gateway.prepare(
        "slow_tool",
        {"value": "x"},
        {"tool_call_id": "runtime_call_001"},
    )
    coordinator = ToolCoordinator(
        default_timeout_secs=0.01,
        offload_on_deadline=True,
    )
    outer = ToolCoordinatorMiddleware(coordinator)
    call = ToolCallBlock(
        id="model_slow_001",
        name="slow_tool",
        input=json.dumps({"value": "x"}),
    )

    async def execute(**_kwargs):
        client.events.append(("execute",))
        await asyncio.sleep(0.05)
        yield ToolResponse(
            id="model_slow_001",
            content=[],
            state=ToolResultState.SUCCESS,
        )

    async def through_gateway(**kwargs):
        async for item in gateway.on_acting(
            SimpleNamespace(),
            kwargs,
            execute,
        ):
            yield item

    output = [
        item
        async for item in outer.on_acting(
            SimpleNamespace(
                _request_context={
                    "session_id": "session_001",
                    "agent_id": "bank-assistant",
                    "root_session_id": "session_001",
                }
            ),
            {"tool_call": call},
            through_gateway,
        )
    ]
    assert output
    await asyncio.sleep(0.1)
    assert [event[0] for event in client.events] == ["guard", "execute", "result"]


def _blocked_tool_names() -> set[str]:
    agent = SimpleNamespace(
        toolkit=SimpleNamespace(
            tool_groups=[
                SimpleNamespace(
                    tools=[
                        SimpleNamespace(name="run_tool_batch", is_external_tool=False),
                        SimpleNamespace(name="policy_search", is_external_tool=False),
                    ]
                )
            ]
        )
    )
    original = {tool.name for tool in agent.toolkit.tool_groups[0].tools}
    managed = {
        tool.name
        for tool in agent.toolkit.tool_groups[0].tools
        if tool.name != "run_tool_batch" and not tool.is_external_tool
    }
    return original - managed


@pytest.mark.asyncio
@pytest.mark.parametrize("with_config", [False, True])
async def test_preflight_failure_denies_without_logging_exception_payload(caplog, with_config):
    class FailingClient(_Client):
        async def preflight(self, *args, **kwargs):
            raise GatewayError("secret-token private-document-content")

    client = FailingClient()
    if with_config:
        client.config = SimpleNamespace(task_id="task-preflight-failed")
    middleware = BankRuntimeGatewayMiddleware(client)
    engine = GatewayPermissionEngine(
        _DelegateEngine(PermissionBehavior.ALLOW, client.events), middleware,
    )
    tool = SimpleNamespace(name="runtime_sandbox_files_search", is_external_tool=False)

    decision = await engine.check_permission(tool, {"query": "private-query"})

    assert decision.behavior == PermissionBehavior.DENY
    assert client.events == []
    assert "GatewayError" in caplog.text
    assert "secret-token" not in caplog.text
    assert "private-document-content" not in caplog.text
    assert "private-query" not in caplog.text
