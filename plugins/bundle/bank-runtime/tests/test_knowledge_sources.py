import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bank_runtime.gateway.knowledge_sources import extract_knowledge_sources, is_knowledge_tool, citation_result


def business():
    return {"code": 0, "data": {"chunks": [{"id": "a", "dataset_id": "d", "document_id": "f",
        "document_keyword": "规则.xlsx", "content": "账户需要核验。", "highlight": "<em>账户</em>需要核验。"}],
        "doc_aggs": [], "total": 1}}


@pytest.mark.parametrize("wrap", [lambda value: value, lambda value: {"structuredContent": value},
    lambda value: {"content": [{"type": "text", "text": json.dumps(value)}]},
    lambda value: [{"type": "text", "text": json.dumps(value)}]])
def test_extracts_explicit_mcp_envelopes(wrap):
    sources, error = extract_knowledge_sources(wrap(business()))
    assert not error
    assert sources[0]["title"] == "规则.xlsx"
    assert sources[0]["highlights"] == ["账户"]
    assert "dataset_id" not in sources[0]


def test_errors_are_not_empty_success_and_invalid_results_fail_closed():
    assert extract_knowledge_sources({"code": 0, "data": {"chunks": []}}) == ([], "")
    assert extract_knowledge_sources({"code": 1, "error_code": "MISSING_SCOPE"}) == ([], "MISSING_SCOPE")
    assert extract_knowledge_sources({"error_code": "MISSING_SCOPE", "hint": "ignored"}) == ([], "MISSING_SCOPE")
    assert extract_knowledge_sources({"error_codes": ["MISSING_SCOPE"]}) == ([], "MISSING_SCOPE")
    assert extract_knowledge_sources({"isError": True, "content": [{"type": "text", "text": '{"error_code":"MISSING_SCOPE"}'}]}) == ([], "MISSING_SCOPE")
    assert extract_knowledge_sources({"isError": True, "structuredContent": business()})[1]
    assert extract_knowledge_sources({"code": 0, "data": {"chunks": "oops"}})[1]


def test_only_known_tools_and_safe_highlights():
    assert is_knowledge_tool("rag-mcp-tools-prod__search_engine_ragflow_recall_tool")
    assert is_knowledge_tool("rag-mcp-tools-local__search_engine_ragflow_recall_tool")
    assert not is_knowledge_tool("untrusted__search_engine_ragflow_recall_tool")
    value = business()
    value["data"]["chunks"][0]["highlight"] = '<em><img src=x onerror=alert(1)></em>'
    assert extract_knowledge_sources(value)[0][0]["highlights"] == []


def test_citation_guidance_respects_profile_defaults_without_discarding_sources():
    from bank_runtime.personalization import _compile_profile
    source = {"source_id": "ks_" + "a" * 32, "snippet": "已授权的资料"}
    for style in ("none", "source_first", "footnote"):
        profile = _compile_profile({"user_overlay": {"profile": {"trust_level": "low", "schema_version": "1.0",
            "preferences": {"citation_style": style}}}})
        guidance = citation_result([source])
        assert f"citation_style: {style}" in profile
        assert "本轮用户的明确要求" in guidance
        assert "none省略可选的正文引用标记" in guidance
        assert "source_first先简要说明主要资料依据" in guidance
        assert "footnote在相关段落末使用下述标记" in guidance
        assert source["source_id"] in guidance and source["snippet"] in guidance


@pytest.mark.asyncio
@pytest.mark.parametrize("result, expected", [(business(), "completed"), ({"code": 403, "error_code": "MISSING_SCOPE"}, "failed")])
async def test_governed_middleware_registers_sources_before_model_can_cite(result, expected):
    from test_gateway_middleware import _Client, _DelegateEngine
    from bank_runtime.gateway.middleware import BankRuntimeGatewayMiddleware, GatewayPermissionEngine
    from agentscope.message import ToolCallBlock, ToolResultState
    from agentscope.permission import PermissionBehavior
    from agentscope.tool import ToolResponse

    class Client(_Client):
        async def report_result(self, call, status, duration, error="", **kwargs):
            self.events.append(("result", status, error, kwargs))
            rows = [{"source_id": "ks_" + "a" * 32, **row} for row in kwargs.get("knowledge_sources", [])]
            return {"status": status, "knowledge_sources": rows}

    client = Client()
    middleware = BankRuntimeGatewayMiddleware(client)
    engine = GatewayPermissionEngine(_DelegateEngine(PermissionBehavior.ALLOW, client.events), middleware)
    name = "rag-mcp-tools-prod__search_engine_ragflow_recall_tool"
    # DriverCapabilityTool exposes governed MCP tools with is_external_tool=False;
    # True denotes AgentScope external execution, which must remain blocked.
    decision = await engine.check_permission(SimpleNamespace(name=name, is_external_tool=False), {"question": "核验"})
    assert decision.behavior == PermissionBehavior.ALLOW

    async def execute(**kwargs):
        client.events.append(("execute",))
        yield ToolResponse(id="call", content=[{"type": "text", "text": json.dumps(result)}], state=ToolResultState.SUCCESS)

    responses = [item async for item in middleware.on_acting(SimpleNamespace(),
        {"tool_call": ToolCallBlock(id="call", name=name, input=json.dumps({"question": "核验"}))}, execute)]
    assert [event[0] for event in client.events] == ["preflight", "tool_guard", "guard", "execute", "result"]
    assert client.events[-1][1] == expected
    if expected == "completed":
        assert "[[kb:" in responses[-1].content[0].text
        assert "ks_" + "a" * 32 in responses[-1].content[0].text
    else:
        assert responses[-1].state == ToolResultState.ERROR
        assert client.events[-1][2] == "MISSING_SCOPE"


@pytest.mark.asyncio
async def test_result_retry_outbox_retains_bounded_snapshot_without_reexecuting(tmp_path):
    from test_gateway_client import _config
    from bank_runtime.gateway.client import GatewayClient
    from bank_runtime.gateway.outbox import GatewayResultOutbox
    client = GatewayClient(_config(), outbox=GatewayResultOutbox(tmp_path))
    sources, _ = extract_knowledge_sources(business())

    async def fail(payload):
        raise OSError("offline")

    client._post = fail
    receipt = await client.report_result("call", "completed", 10, knowledge_sources=sources)
    assert receipt["status"] == "result_pending"
    assert client.outbox.pending(client.config.task_id)[0]["knowledge_sources"] == sources
    delivered = []

    async def success(payload):
        delivered.append(payload)
        return {"tool_call_id": "call", "status": "completed"}

    client._post = success
    await client._flush_pending_results()
    assert delivered[0]["knowledge_sources"] == sources
    assert not client.outbox.pending(client.config.task_id)
