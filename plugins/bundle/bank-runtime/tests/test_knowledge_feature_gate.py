"""Citation opt-in never becomes a prerequisite for legacy MCP retrieval."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentscope.message import TextBlock, ToolCallBlock, ToolResultState
from agentscope.permission import PermissionBehavior
from agentscope.tool import ToolResponse
from bank_runtime.gateway import middleware as module
from test_gateway_middleware import _Client, _DelegateEngine

FLAG = "QWENPAW_RUNTIME_KNOWLEDGE_CITATIONS_ENABLED"
NAME = "rag-mcp-tools-prod__search_engine_ragflow_recall_tool"


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    '{"chunks":[{"content":"正常正文"}]}',
    '{"chunks":[{"content":"broken\\q"}]}',
    json.dumps({"chunks": [{"content": "x" * 2_000_001}]}),
], ids=["valid", "malformed", "oversized"])
async def test_default_off_preserves_raw_success_and_never_parses(monkeypatch, payload):
    def unexpected_parse(*args):
        pytest.fail("disabled citation path must not parse MCP content")
    monkeypatch.setattr(module, "extract_knowledge_sources", unexpected_parse)
    client = _Client()
    middleware = module.BankRuntimeGatewayMiddleware(client)
    engine = module.GatewayPermissionEngine(_DelegateEngine(PermissionBehavior.ALLOW, client.events), middleware)
    await engine.check_permission(SimpleNamespace(name=NAME, is_external_tool=False), {"question": "核验"})
    content = [TextBlock(type="text", text=payload), TextBlock(type="text", text="独立文本块")]
    original = ToolResponse(id="call", content=content, state=ToolResultState.SUCCESS)

    async def execute(**kwargs):
        yield original

    responses = [item async for item in middleware.on_acting(SimpleNamespace(),
        {"tool_call": ToolCallBlock(id="call", name=NAME, input=json.dumps({"question": "核验"}))}, execute)]
    assert responses[-1] is original
    assert responses[-1].content[0].text == payload
    assert len(responses[-1].content) == 2
    assert responses[-1].state == ToolResultState.SUCCESS
    # This legacy client deliberately accepts no knowledge_sources argument.
    assert client.events[-1][2:] == ("completed", "")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["error", "timeout", "cancelled"])
async def test_disabled_citations_preserve_real_transport_failures(monkeypatch, failure):
    monkeypatch.setattr(module, "extract_knowledge_sources", lambda *_: pytest.fail("must not parse"))
    client = _Client()
    middleware = module.BankRuntimeGatewayMiddleware(client)
    engine = module.GatewayPermissionEngine(_DelegateEngine(PermissionBehavior.ALLOW, client.events), middleware)
    await engine.check_permission(SimpleNamespace(name=NAME, is_external_tool=False), {})

    async def execute(**kwargs):
        if failure == "timeout":
            raise TimeoutError("synthetic upstream timeout")
        if failure == "cancelled":
            raise module.asyncio.CancelledError()
        yield ToolResponse(id="call", content=[TextBlock(text='{"error_code":"MISSING_SCOPE"}')], state=ToolResultState.ERROR)

    async def run():
        return [item async for item in middleware.on_acting(SimpleNamespace(),
            {"tool_call": ToolCallBlock(id="call", name=NAME, input="{}")}, execute)]

    if failure == "cancelled":
        with pytest.raises(module.asyncio.CancelledError):
            await run()
        assert client.events[-1][2] == "cancelled"
    elif failure == "timeout":
        with pytest.raises(module.GatewayError) as raised:
            await run()
        assert isinstance(raised.value.__cause__, TimeoutError)
        assert client.events[-1][2] == "failed"
    else:
        responses = await run()
        assert responses[-1].state == ToolResultState.ERROR
        assert client.events[-1][2] == "failed"


@pytest.mark.parametrize("value,enabled", [(None, False), ("false", False), ("true", True), ("yes", False), ("1", False), ("garbage", False)])
def test_factory_uses_environment_only_and_freezes_value(monkeypatch, value, enabled):
    from bank_runtime.sandbox.executor import RuntimeSandboxExecutor
    monkeypatch.setattr(RuntimeSandboxExecutor, "from_request", lambda **kwargs: None)
    if value is None:
        monkeypatch.delenv(FLAG, raising=False)
    else:
        monkeypatch.setenv(FLAG, value)
    gateway = {"protocol":"preflight_guard_result_v2", "base_url":"http://127.0.0.1:8765",
        "endpoint":"/runtime/v1/tool-calls", "token":"test-only", "task_id":"task_001",
        "session_id":"session_001", "tool_session_id":"wts_001", "policy_snapshot_id":"policy_001",
        "task_scope_id":"scope_001", "capability_snapshot_hash":"sha256:capability",
        "worker_protocol_version":"runtime-worker/v1", "trace_id":"trace_001", "worker_agent_id":"bank-assistant"}
    request = SimpleNamespace(channel="bank-runtime", runtime_tool_gateway=gateway,
        knowledge_citations_enabled=True, request_context={"runtime_tool_gateway":gateway, "knowledge_citations_enabled":True})
    middleware = module.bank_runtime_middleware_factory(SimpleNamespace(request=request, agent_id="bank-assistant"),
        SimpleNamespace(knowledge_citations_enabled=True))
    assert not middleware.configuration_error
    assert middleware.knowledge_citations_enabled is enabled
    monkeypatch.setenv(FLAG, "false" if enabled else "true")
    assert middleware.knowledge_citations_enabled is enabled
