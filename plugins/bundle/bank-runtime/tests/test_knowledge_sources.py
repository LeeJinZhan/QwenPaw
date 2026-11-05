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
        "doc_aggs": {"f": {"doc_name": "规则.xlsx", "count": 1}}, "total": 1}}


def standard_result():
    return {"schema_version": "knowledge-retrieval/v1", "status": "success", "sources": [
        {"document_id": "engine:collection:doc-a", "chunk_id": "a", "title": "规则.xlsx",
         "content": "账户需要核验。", "highlights": ["账户"]}
    ]}


@pytest.mark.parametrize("wrap", [lambda value: value, lambda value: {"structuredContent": value},
    lambda value: {"content": [{"type": "text", "text": json.dumps(value)}]},
    lambda value: [{"type": "text", "text": json.dumps(value)}]])
@pytest.mark.parametrize("top_level", [False, True])
def test_extracts_explicit_mcp_envelopes(wrap, top_level):
    value = business()["data"] if top_level else business()
    sources, error = extract_knowledge_sources(wrap(value))
    assert not error
    assert sources[0]["title"] == "规则.xlsx"
    assert sources[0]["highlights"] == ["账户"]
    assert "dataset_id" not in sources[0]


@pytest.mark.parametrize("value", [business()["data"], standard_result()])
def test_top_level_chunks_survive_real_mcp_to_driver_text_conversion(value):
    from mcp.types import CallToolResult, TextContent
    from qwenpaw.drivers.adapters.agentscope_tool import _blocks_from_value

    response = CallToolResult(content=[TextContent(type="text", text=json.dumps(value))],
                              structuredContent=value, isError=False)
    expected = extract_knowledge_sources(value)
    assert extract_knowledge_sources(response) == expected
    assert extract_knowledge_sources(_blocks_from_value(response)) == expected


def test_top_level_empty_and_content_only_chunks():
    assert extract_knowledge_sources({"chunks": []}) == ([], "")
    sources, error = extract_knowledge_sources({"chunks": [{"content": "检索确实返回的正文"}]})
    assert not error
    assert sources[0]["snippet"] == "检索确实返回的正文"
    assert sources[0]["title"] == "知识库文档"


@pytest.mark.parametrize("failure, expected", [
    ({"code": 503}, "KNOWLEDGE_RETRIEVAL_FAILED"),
    ({"code": False}, "KNOWLEDGE_RETRIEVAL_FAILED"),
    ({"error_code": "MISSING_SCOPE"}, "MISSING_SCOPE"),
    ({"code": 0, "error_codes": ["MISSING_SCOPE"]}, "MISSING_SCOPE"),
    ({"error_code": "RETRIEVAL_UNAVAILABLE"}, "KNOWLEDGE_RETRIEVAL_FAILED"),
    ({"code": 0, "error_code": "RETRIEVAL_UNAVAILABLE"}, "KNOWLEDGE_RETRIEVAL_FAILED"),
    ({"isError": True}, "KNOWLEDGE_RETRIEVAL_FAILED"),
])
def test_top_level_chunks_never_override_explicit_failure(failure, expected):
    assert extract_knowledge_sources({**business()["data"], **failure}) == ([], expected)


@pytest.mark.parametrize("value", [{"chunks": "invalid"}, {"chunks": [None]}, {"unrelated": []}])
def test_top_level_malformed_results_remain_invalid(value):
    assert extract_knowledge_sources(value) == ([], "KNOWLEDGE_RESULT_INVALID")


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
@pytest.mark.parametrize("result, expected, expected_error", [
    (business(), "completed", ""), (business()["data"], "completed", ""),
    (standard_result(), "completed", ""),
    ({"schema_version": "knowledge-retrieval/v1", "status": "empty", "sources": []}, "completed", ""),
    ({"chunks": [], "error": None, "note": "范围内无文档"}, "completed", ""),
    ({"chunks": [], "error": "范围解析失败"}, "failed", "KNOWLEDGE_RETRIEVAL_FAILED"),
    ({"error": "网络异常"}, "failed", "KNOWLEDGE_RETRIEVAL_FAILED"),
    ({"schema_version": "knowledge-retrieval/v1", "status": "error", "error": {"code": "MISSING_SCOPE"}}, "failed", "MISSING_SCOPE"),
    ({"code": 403, "error_code": "MISSING_SCOPE"}, "failed", "MISSING_SCOPE"),
    ({**business()["data"], "error_code": "MISSING_SCOPE"}, "failed", "MISSING_SCOPE")])
async def test_governed_middleware_registers_sources_before_model_can_cite(result, expected, expected_error):
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
    middleware = BankRuntimeGatewayMiddleware(client, knowledge_citations_enabled=True)
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
        if client.events[-1][3]["knowledge_sources"]:
            assert "[[kb:" in responses[-1].content[0].text
            assert "ks_" + "a" * 32 in responses[-1].content[0].text
        else:
            assert "未命中" in responses[-1].content[0].text
            assert "[[kb:" not in responses[-1].content[0].text
    else:
        assert responses[-1].state == ToolResultState.ERROR
        assert client.events[-1][2] == expected_error


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



@pytest.mark.parametrize("wrap", [lambda value: value, lambda value: json.dumps(value),
    lambda value: {"content": [{"type": "text", "text": json.dumps(value)}]},
    lambda value: {"structuredContent": value}])
def test_standard_contract_is_engine_independent_and_ignores_optional_metadata(wrap):
    value = standard_result()
    value["vendor_metadata"] = {"future_field": "ignored"}
    value["sources"][0]["internal_url"] = "http://internal/not-public"
    sources, error = extract_knowledge_sources(wrap(value))
    assert not error
    assert sources == [{"document_key": "engine:collection:doc-a", "chunk_key": "a",
        "title": "规则.xlsx", "snippet": "账户需要核验。", "highlights": ["账户"]}]


@pytest.mark.parametrize("value, error", [
    ({"schema_version": "knowledge-retrieval/v2", "chunks": []}, "KNOWLEDGE_RESULT_VERSION_UNSUPPORTED"),
    ({"schema_version": "knowledge-retrieval/v1", "status": "empty", "sources": []}, ""),
    ({"schema_version": "knowledge-retrieval/v1", "status": "error", "error": {"code": "MISSING_SCOPE"}}, "MISSING_SCOPE"),
    ({"schema_version": "knowledge-retrieval/v1", "status": "error", "error": {"code": "UPSTREAM_UNAVAILABLE"}}, "KNOWLEDGE_RETRIEVAL_FAILED"),
    ({**standard_result(), "error": {"code": "UPSTREAM_UNAVAILABLE"}}, "KNOWLEDGE_RETRIEVAL_FAILED"),
    ({**standard_result(), "status": "empty"}, "KNOWLEDGE_RESULT_INVALID"),
    ({**standard_result(), "sources": []}, "KNOWLEDGE_RESULT_INVALID"),
    ({**standard_result(), "status": "other"}, "KNOWLEDGE_RESULT_INVALID"),
    ({**standard_result(), "status": []}, "KNOWLEDGE_RESULT_INVALID"),
])
def test_standard_status_error_and_version_semantics(value, error):
    assert extract_knowledge_sources(value) == ([], error)


@pytest.mark.parametrize("change", [{"document_id": ""}, {"chunk_id": ""}, {"title": 123},
    {"content": " "}, {"highlights": "bad"}, {"highlights": [None]}])
def test_standard_rejects_missing_identity_and_invalid_fields(change):
    value = standard_result()
    value["sources"][0].update(change)
    assert extract_knowledge_sources(value) == ([], "KNOWLEDGE_RESULT_INVALID")


@pytest.mark.parametrize("payload", [
    {"error": "upstream failure"},
    {"chunks": [], "error": "scope resolution failed"},
    {"chunks": [], "error": None, "department_scope": {"error": "missing datasets"}},
    {"code": 0, "data": {"chunks": [], "error": "retrieval failed"}},
])
def test_legacy_errors_are_never_downgraded_to_empty_success(payload):
    assert extract_knowledge_sources(json.dumps(payload)) == ([], "KNOWLEDGE_RETRIEVAL_FAILED")


def test_legacy_scope_short_circuit_is_normal_empty():
    value = {"chunks": [], "total": 0, "error": None,
             "note": "可阅范围内无文档，未执行检索", "department_scope": {"error": None}}
    assert extract_knowledge_sources(json.dumps(value)) == ([], "")


def test_legacy_aliases_aggregation_and_unknown_documents_do_not_merge():
    value = {"chunks": [{"chunk_id": "c1", "kb_id": "kb", "doc_id": "d1", "content": "正文一"},
                         {"chunk_id": "c2", "kb_id": "kb", "doc_id": "d2", "docnm_kwd": "文档二", "content": "正文二"}],
             "doc_aggs": {"d1": {"doc_name": "文档一", "count": 1}}}
    sources, error = extract_knowledge_sources(value)
    assert not error
    assert [s["title"] for s in sources] == ["文档一", "文档二"]
    assert [s["chunk_key"] for s in sources] == ["c1", "c2"]
    assert sources[0]["document_key"] != sources[1]["document_key"]
    unknown, _ = extract_knowledge_sources({"chunks": [{"content": "未知一"}, {"content": "未知二"}]})
    assert unknown[0]["document_key"] != unknown[1]["document_key"]


def test_standard_sources_are_bounded_and_highlights_are_plain_matching_text():
    value = standard_result()
    value["sources"][0]["content"] = "账户" * 5000
    value["sources"][0]["highlights"] = ["账户", "账户", "<em>账户</em>", "不存在"]
    value["sources"] *= 30
    sources, error = extract_knowledge_sources(value)
    assert not error and 0 < len(sources) <= 20
    assert all(len(s["snippet"]) <= 6000 and s["highlights"] == ["账户"] for s in sources)
    assert len(json.dumps(sources, ensure_ascii=False).encode()) <= 65536


def test_legacy_supported_document_identity_is_stable():
    import hashlib
    sources, error = extract_knowledge_sources(business())
    expected = hashlib.sha256(json.dumps(["d", "f", "规则.xlsx"], ensure_ascii=False).encode()).hexdigest()
    assert not error and sources[0]["document_key"] == expected


@pytest.mark.parametrize("value", [standard_result(), business()["data"]])
def test_mcp_failure_never_publishes_successful_sources(value):
    from mcp.types import CallToolResult, TextContent
    result = CallToolResult(content=[TextContent(type="text", text=json.dumps(value))], isError=True)
    assert extract_knowledge_sources(result) == ([], "KNOWLEDGE_RETRIEVAL_FAILED")
