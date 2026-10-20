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

    async def preflight(self, tool_name, tool_input, *, call_id, refresh=None):
        self.events.append(("refresh" if refresh is not None else "preflight", tool_name, dict(tool_input), call_id))
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
    assert "artifact_job_001" not in output[0].content[0].text
    assert "generated_file_001" in output[0].content[0].text
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

@pytest.mark.asyncio
async def test_pdf_confirmation_is_identical_for_preflight_guard_and_execution():
    from bank_runtime.artifact_tools import ArtifactDeliveryIntent
    client = _Client()
    middleware = BankRuntimeGatewayMiddleware(client, artifact_intent=ArtifactDeliveryIntent('convert', 'pdf'))
    engine = GatewayPermissionEngine(_DelegateEngine(PermissionBehavior.ALLOW, client.events), middleware)
    raw = {'source_generated_file_id': 'source1', 'target_format': 'pdf'}
    decision = await engine.check_permission(SimpleNamespace(name='artifact_convert'), raw)
    assert decision.behavior == PermissionBehavior.ALLOW
    async def forbidden(**kwargs):
        pytest.fail('conversion must execute through Runtime')
        yield
    call = ToolCallBlock(id='c1', name='artifact_convert', input=json.dumps(raw))
    results = [item async for item in middleware.on_acting(SimpleNamespace(), {'tool_call': call}, forbidden)]
    assert results
    assert client.events[0][2]['explicit_pdf_request'] is True
    assert client.events[1][2]['explicit_pdf_request'] is True
    assert client.events[-1][3] == client.events[0][2]
    assert 'explicit_pdf_request' not in raw


@pytest.mark.asyncio
async def test_repeated_artifact_input_rejections_stop_only_same_operation():
    from agentscope.model import ChatResponse
    class InvalidClient(_Client):
        calls = 0
        async def preflight(self, *args, **kwargs):
            self.calls += 1
            raise GatewayError("invalid", code="INVALID_REQUEST", violation="tool_input_field_not_allowed")
    client = InvalidClient()
    middleware = BankRuntimeGatewayMiddleware(client)
    engine = GatewayPermissionEngine(_DelegateEngine(PermissionBehavior.ALLOW, client.events), middleware)
    tool = SimpleNamespace(name="artifact_generate")
    for _ in range(3):
        decision = await engine.check_permission(tool, {"artifact_type": "docx"})
        assert decision.behavior == PermissionBehavior.DENY
    called = False
    async def model(**kwargs):
        nonlocal called
        called = True
        return ChatResponse(content=[ToolCallBlock(id="independent", name="chart_export", input='{"chart_id":"chart-a"}')], is_last=True)
    await middleware.on_model_call(None, {}, model)
    assert called
    assert client.calls == 2
    with pytest.raises(Exception) as caught:
        middleware._check_file_completion()
    assert getattr(caught.value, "error_code", "") == "ARTIFACT_VALIDATION_FAILED"


@pytest.mark.asyncio
async def test_admission_does_not_reset_parameter_failure_streak():
    client = _Client()
    middleware = BankRuntimeGatewayMiddleware(client)
    middleware.artifact_input_failures = 1
    engine = GatewayPermissionEngine(_DelegateEngine(PermissionBehavior.ALLOW, client.events), middleware)
    decision = await engine.check_permission(SimpleNamespace(name="artifact_generate"), {"artifact_type": "docx"})
    assert decision.behavior == PermissionBehavior.ALLOW
    assert middleware.artifact_input_failures == 1


@pytest.mark.asyncio
async def test_permission_failure_is_not_misreported_as_invalid_artifact_input():
    class DeniedClient(_Client):
        async def preflight(self, *args, **kwargs):
            raise GatewayError("denied", code="FORBIDDEN")
    client = DeniedClient()
    middleware = BankRuntimeGatewayMiddleware(client)
    engine = GatewayPermissionEngine(_DelegateEngine(PermissionBehavior.ALLOW, client.events), middleware)
    for _ in range(3):
        decision = await engine.check_permission(SimpleNamespace(name="artifact_generate"), {"artifact_type": "docx"})
        assert decision.behavior == PermissionBehavior.DENY
    assert middleware.artifact_input_failures == 0


@pytest.mark.asyncio
async def test_execution_validation_failures_share_the_preflight_budget():
    from agentscope.model import ChatResponse
    class Client(_Client):
        async def execute_runtime_tool(self, *args):
            return {"status": "failed", "execution_status": "not_started", "error_code": "ARTIFACT_VALIDATION_FAILED", "result": {"artifact_status": "failed"}}
    client = Client()
    middleware = BankRuntimeGatewayMiddleware(client)
    engine = GatewayPermissionEngine(_DelegateEngine(PermissionBehavior.ALLOW, client.events), middleware)
    async def forbidden(**kwargs):
        raise AssertionError("local execution forbidden")
        yield
    for i in range(2):
        payload = {"artifact_type": "docx"}
        await engine.check_permission(SimpleNamespace(name="artifact_generate"), payload)
        call = ToolCallBlock(id=f"call-{i}", name="artifact_generate", input=json.dumps(payload))
        _ = [item async for item in middleware.on_acting(SimpleNamespace(), {"tool_call": call}, forbidden)]
    assert middleware.artifact_input_failures == 2
    assert (await engine.check_permission(SimpleNamespace(name="artifact_generate"), payload)).behavior == PermissionBehavior.DENY
    assert (await engine.check_permission(SimpleNamespace(name="artifact_generate"), {"artifact_type": "pptx"})).behavior == PermissionBehavior.ALLOW
    async def independent_model(**kwargs):
        return ChatResponse(content=[ToolCallBlock(id="independent", name="artifact_generate", input='{"artifact_type":"pptx"}')], is_last=True)
    await middleware.on_model_call(None, {}, independent_model)
    with pytest.raises(Exception) as error:
        middleware._check_file_completion()
    assert getattr(error.value, "error_code", "") == "ARTIFACT_VALIDATION_FAILED"


@pytest.mark.asyncio
async def test_exhausted_renderer_layout_stops_same_operation_and_preserves_final_failure():
    from agentscope.model import ChatResponse
    from bank_runtime.gateway.client import _response_error
    class Client(_Client):
        executions = 0
        async def execute_runtime_tool(self, *args):
            self.executions += 1
            raise _response_error({"code": "ARTIFACT_VALIDATION_FAILED", "details": {
                "reason": "presentation_layout_capacity", "retryable": False,
                "page_index": 5, "layout": "chart", "element": "chart_conclusion",
            }}, "failed")
    client = Client()
    middleware = BankRuntimeGatewayMiddleware(client)
    async def forbidden(**kwargs):
        raise AssertionError("must not execute locally")
        yield
    for i in range(2):
        payload = {"artifact_type": "pptx", "output_name": "报告.pptx"}
        middleware.prepare("artifact_generate", payload, {"tool_call_id": f"call-{i}"})
        call = ToolCallBlock(id=f"call-{i}", name="artifact_generate", input=json.dumps(payload))
        with pytest.raises(Exception):
            _ = [x async for x in middleware.on_acting(None, {"tool_call": call}, forbidden)]
    assert client.executions == 1
    async def independent_model(**kwargs):
        return ChatResponse(content=[ToolCallBlock(id="independent", name="artifact_generate", input='{"artifact_type":"docx"}')], is_last=True)
    await middleware.on_model_call(None, {}, independent_model)
    with pytest.raises(Exception) as raised:
        middleware._raise_layout_failure()
    assert raised.value.message == "PPTX_LAYOUT_CAPACITY|5|chart_conclusion"


def test_factory_captures_trusted_runtime_model_policy(monkeypatch):
    from bank_runtime.sandbox.executor import RuntimeSandboxExecutor
    monkeypatch.setattr(RuntimeSandboxExecutor, 'from_request', lambda **kwargs: None)
    gateway = {'protocol':'preflight_guard_result_v2', 'base_url':'http://127.0.0.1:8765',
        'endpoint':'/runtime/v1/tool-calls', 'token':'test-only', 'task_id':'task_001',
        'session_id':'session_001', 'tool_session_id':'wts_001', 'policy_snapshot_id':'policy_001',
        'task_scope_id':'scope_001', 'capability_snapshot_hash':'sha256:capability',
        'worker_protocol_version':'runtime-worker/v1', 'trace_id':'trace_001', 'worker_agent_id':'bank-assistant'}
    policy = {'idle_seconds':300, 'no_output_retry_attempts':0, 'truncation_recovery_attempts':0}
    request = SimpleNamespace(channel='bank-runtime', runtime_tool_gateway=gateway,
        request_context={'runtime_tool_gateway':gateway,'runtime_model_budget_seconds':900,'runtime_model_policy':policy})
    middleware = bank_runtime_middleware_factory(SimpleNamespace(request=request, agent_id='bank-assistant'), SimpleNamespace())
    assert not middleware.configuration_error
    assert middleware.model_reliability.idle_seconds == 300
    assert middleware.model_reliability.no_output_retry is False
    assert middleware.model_reliability.truncation_recovery is False
    policy['idle_seconds'] = 900
    assert middleware.model_reliability.idle_seconds == 300


@pytest.mark.asyncio
async def test_core_guard_wait_rechecks_the_same_call_and_denies_revocation():
    class RefreshDenied(_Client):
        async def preflight(self, tool_name, tool_input, *, call_id, refresh=None):
            if refresh is not None:
                assert refresh["tool_call_id"] == "runtime_call_001"
                raise GatewayError("Permission revoked")
            result = await super().preflight(tool_name, tool_input, call_id=call_id)
            result["permit"]["payload"]["expires_at"] = "2000-01-01T00:00:00+00:00"
            return result
    client = RefreshDenied()
    middleware = BankRuntimeGatewayMiddleware(client)
    engine = GatewayPermissionEngine(_DelegateEngine(PermissionBehavior.ALLOW, client.events), middleware)
    decision = await engine.check_permission(SimpleNamespace(name="policy_search", is_external_tool=False), {"query":"example"})
    assert decision.behavior == PermissionBehavior.DENY
    assert not middleware._prepared

@pytest.mark.asyncio
async def test_guard_report_failure_is_observable_and_cannot_prepare_execution():
    class ReportFailed(_Client):
        async def report_guard(self, preflight, decision):
            raise GatewayError("transport failed")
    client = ReportFailed()
    middleware = BankRuntimeGatewayMiddleware(client)
    engine = GatewayPermissionEngine(_DelegateEngine(PermissionBehavior.DENY, client.events), middleware)
    with pytest.raises(GatewayError, match="同步失败"):
        await engine.check_permission(SimpleNamespace(name="policy_search", is_external_tool=False), {"query":"example"})
    assert not middleware._prepared

@pytest.mark.asyncio
async def test_guard_wrapper_keeps_server_authorization_refusal():
    denial = GatewayError('safe authorization refusal', code='FORBIDDEN', violation='user_authority_unverified', http_status=403)
    async def report_guard(*args): raise denial
    engine = object.__new__(GatewayPermissionEngine)
    engine.middleware = SimpleNamespace(client=SimpleNamespace(report_guard=report_guard))
    with pytest.raises(GatewayError) as caught:
        await engine._report_guard_safely({'tool_call_id': 'runtime_call_001'}, 'allow')
    assert caught.value is denial
    assert caught.value.violation == 'user_authority_unverified'

@pytest.mark.asyncio
@pytest.mark.parametrize('fixed_rejection', [True, False])
async def test_actual_allow_ack_path_preserves_denial_without_executing_handler(fixed_rejection):
    client = _Client()
    middleware = BankRuntimeGatewayMiddleware(client)
    engine = GatewayPermissionEngine(_DelegateEngine(PermissionBehavior.ALLOW, client.events), middleware)
    decision = await engine.check_permission(SimpleNamespace(name='policy_search', is_external_tool=False), {'query': 'public'})
    assert decision.behavior == PermissionBehavior.ALLOW
    async def refused_guard(*args):
        if fixed_rejection:
            raise GatewayError('private identity', code='FORBIDDEN', violation='user_authority_unverified', http_status=403,
                               failure_metadata={'execution_status': 'not_started_cancelled', 'retryable': False})
        raise OSError('private transport path')
    client.report_guard = refused_guard
    executed = []
    async def next_handler(**kwargs):
        executed.append(kwargs)
        yield ToolResponse(id='model_call_001', content=[], state=ToolResultState.SUCCESS)
    call = ToolCallBlock(id='model_call_001', name='policy_search', input=json.dumps({'query': 'public'}))
    with pytest.raises(GatewayError) as caught:
        _ = [item async for item in middleware.on_acting(SimpleNamespace(), {'tool_call': call}, next_handler)]
    assert executed == []
    assert caught.value.code == ('FORBIDDEN' if fixed_rejection else 'TOOL_GUARD_REPORT_FAILED')
    assert caught.value.execution_status == ('not_started_cancelled' if fixed_rejection else 'not_started')
    assert caught.value.failure_metadata['retryable'] is False
    if fixed_rejection:
        assert caught.value.violation == 'user_authority_unverified'
        assert caught.value.http_status == 403
        assert '服务端认证' in str(caught.value)
    assert 'private' not in str(caught.value)

@pytest.mark.asyncio
async def test_permit_refresh_denial_preserves_unverified_identity_public_message():
    class UnverifiedAfterWait(_Client):
        async def preflight(self, tool_name, tool_input, *, call_id, refresh=None):
            if refresh is not None:
                raise GatewayError('private identity', code='FORBIDDEN', violation='user_authority_unverified', http_status=403)
            result = await super().preflight(tool_name, tool_input, call_id=call_id)
            result['permit']['payload']['expires_at'] = '2000-01-01T00:00:00+00:00'
            return result
    client = UnverifiedAfterWait()
    middleware = BankRuntimeGatewayMiddleware(client)
    engine = GatewayPermissionEngine(_DelegateEngine(PermissionBehavior.ALLOW, client.events), middleware)
    decision = await engine.check_permission(SimpleNamespace(name='policy_search', is_external_tool=False), {'query': 'public'})
    assert decision.behavior == PermissionBehavior.DENY
    assert '服务端认证' in decision.message
    assert '已配置' in decision.message
    assert 'private' not in decision.message
    assert not middleware._prepared
