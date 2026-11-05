"""RAGFlow retrieval envelopes, multi-block atomicity and literal source text."""
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bank_runtime.gateway.knowledge_sources import extract_knowledge_sources


def result(index=0):
    return {"chunks": [{"id": str(index), "dataset_id": "kb", "document_id": "doc",
                        "document_keyword": "制度.pdf", "content": f"第{index}条正文"}]}


def blocks(*values):
    return [{"type": "text", "text": json.dumps(value, ensure_ascii=False)} for value in values]


@pytest.mark.parametrize("wrap", [lambda v: v, lambda v: {"content": v},
                                 lambda v: json.dumps({"content": v})])
def test_all_result_blocks_are_merged_in_order_and_exact_duplicates_removed(wrap):
    sources, error = extract_knowledge_sources(wrap(blocks(result(), result(), result(1))))
    assert not error
    assert [s["snippet"] for s in sources] == ["第0条正文", "第1条正文"]


@pytest.mark.parametrize("metadata", [{"code": 0}, {"error": None}, {"department_scope": {"error": None}}])
def test_neutral_metadata_does_not_mask_later_results(metadata):
    assert extract_knowledge_sources(blocks(metadata, result())) == extract_knowledge_sources(result())


@pytest.mark.parametrize("failure,expected", [
    ({"code": 503}, "KNOWLEDGE_RETRIEVAL_FAILED"),
    ({"error_code": "MISSING_SCOPE"}, "MISSING_SCOPE"),
    ({"department_scope": {"error": "denied"}}, "KNOWLEDGE_RETRIEVAL_FAILED"),
    ({"chunks": [None]}, "KNOWLEDGE_RESULT_INVALID"),
    ({"schema_version": "knowledge-retrieval/v2"}, "KNOWLEDGE_RESULT_VERSION_UNSUPPORTED"),
])
def test_later_failure_prevents_partial_sources_even_after_output_limit(failure, expected):
    assert extract_knowledge_sources(blocks(*(result(i) for i in range(22)), failure)) == ([], expected)


@pytest.mark.parametrize("bad", ['{"chunks":[', '{"error":"bad\\q"}', '```json\n{"error":"denied"}\n```', '\ufeff{"error":"denied"}'])
def test_malformed_later_json_block_is_not_discarded(bad):
    assert extract_knowledge_sources(blocks(result()) + [{"type": "text", "text": bad}]) == ([], "KNOWLEDGE_RESULT_INVALID")


@pytest.mark.parametrize("as_text", [False, True])
def test_native_doc_aggs_list_fills_only_missing_titles(as_text):
    value = result()
    del value["chunks"][0]["document_keyword"]
    value["doc_aggs"] = [{"doc_id": "other", "doc_name": "另一份.pdf"},
                         {"doc_id": "doc", "doc_name": "制度.pdf", "count": 1}]
    wrapped = {"code": 0, "data": value}
    sources, error = extract_knowledge_sources(json.dumps(wrapped) if as_text else wrapped)
    assert not error and sources[0]["title"] == "制度.pdf"


def test_real_sdk_text_blocks_preserve_literal_escapes_and_multiple_positions():
    from mcp.types import CallToolResult, TextContent
    from qwenpaw.drivers.adapters.agentscope_tool import _blocks_from_value
    text = '第一行\n制度"名称"\tC:\\docs\\new\n字面量\\n，中文🙂，<script>alert(1)</script>'
    value = result()
    value["chunks"][0].update(content=text, positions=[[1, 2, 3, 4, 5], [2, 3, 4, 5, 6]])
    response = CallToolResult(content=[TextContent(type="text", text=json.dumps(v)) for v in [value, result(1)]])
    for actual in [response, _blocks_from_value(response)]:
        sources, error = extract_knowledge_sources(actual)
        assert not error and len(sources) == 2
        assert sources[0]["snippet"] == text


def test_repeated_chunk_identity_with_different_body_is_not_silently_lost():
    changed = result()
    changed["chunks"][0]["content"] = "不同正文"
    sources, error = extract_knowledge_sources(blocks(result(), changed))
    assert not error and len(sources) == 2


@pytest.mark.parametrize("body", ["x" * 2_000_001, '{"chunks":[{"content":"\\ud800"}]}'])
def test_oversize_and_unencodable_later_results_fail_without_exception(body):
    assert extract_knowledge_sources(blocks(result()) + [{"type": "text", "text": body}]) == ([], "KNOWLEDGE_RESULT_INVALID")


def test_combined_snapshot_budget_is_global():
    values = [result(i) for i in range(30)]
    for value in values:
        value["chunks"][0]["content"] *= 1500
    sources, error = extract_knowledge_sources(blocks(*values))
    assert not error and 0 < len(sources) <= 20
    assert len(json.dumps(sources, ensure_ascii=False).encode()) <= 65536
    assert all(len(s["snippet"]) <= 6000 for s in sources)


def test_empty_results_and_explanatory_text_do_not_hide_nonempty_results():
    values = [{"type": "text", "text": "以下是检索结果"}] + blocks({"chunks": []}, result())
    assert extract_knowledge_sources(values) == extract_knowledge_sources(result())
    assert extract_knowledge_sources(blocks({"chunks": []}, {"chunks": []})) == ([], "")
    assert extract_knowledge_sources(blocks({"code": 0})) == ([], "KNOWLEDGE_RESULT_INVALID")


def test_existing_title_wins_and_unmatched_aggregates_do_not_supply_a_title():
    value = result()
    value["doc_aggs"] = [{"doc_id": "doc", "doc_name": "其它标题"}]
    assert extract_knowledge_sources(value)[0][0]["title"] == "制度.pdf"
    del value["chunks"][0]["document_keyword"]
    value["doc_aggs"][0]["doc_id"] = "other"
    assert extract_knowledge_sources(value)[0][0]["title"] == "知识库文档"


@pytest.mark.parametrize("failure", [{"isError": True}, {"code": 403}, {"error": "denied"}])
def test_error_envelopes_override_successful_content(failure):
    assert extract_knowledge_sources({"content": blocks(result()), **failure}) == ([], "KNOWLEDGE_RETRIEVAL_FAILED")


def test_structured_content_is_authoritative_and_null_falls_back_to_text():
    assert extract_knowledge_sources({"structuredContent": result(), "content": blocks(result(1))}) == extract_knowledge_sources(result())
    assert extract_knowledge_sources({"structuredContent": None, "content": blocks(result())}) == extract_knowledge_sources(result())


def test_mcp_error_preserves_versioned_scope_error():
    error = {"schema_version": "knowledge-retrieval/v1", "status": "error", "error": {"code": "MISSING_SCOPE"}}
    assert extract_knowledge_sources({"isError": True, "content": blocks(error)}) == ([], "MISSING_SCOPE")


def test_excessively_nested_objects_fail_without_hiding_later_errors():
    value = result()
    for _ in range(10):
        value = {"content": value}
    assert extract_knowledge_sources([result(), value]) == ([], "KNOWLEDGE_RESULT_INVALID")


@pytest.mark.asyncio
@pytest.mark.parametrize("late_error", [False, True])
async def test_multiblock_gateway_ack_publishes_all_sources_or_none(late_error):
    from types import SimpleNamespace
    from test_gateway_middleware import _Client, _DelegateEngine
    from bank_runtime.gateway.middleware import BankRuntimeGatewayMiddleware, GatewayPermissionEngine
    from agentscope.message import ToolCallBlock, ToolResultState
    from agentscope.permission import PermissionBehavior
    from agentscope.tool import ToolResponse
    from mcp.types import CallToolResult, TextContent
    from qwenpaw.drivers.adapters.agentscope_tool import _blocks_from_value

    class Client(_Client):
        async def report_result(self, call, status, duration, error="", **kwargs):
            self.events.append(("result", status, error, kwargs))
            return {"status": status, "knowledge_sources": [
                {"source_id": "ks_" + f"{i:032x}", **row}
                for i, row in enumerate(kwargs.get("knowledge_sources", []), 1)]}

    client = Client()
    middleware = BankRuntimeGatewayMiddleware(client, knowledge_citations_enabled=True)
    engine = GatewayPermissionEngine(_DelegateEngine(PermissionBehavior.ALLOW, client.events), middleware)
    name = "rag-mcp-tools-prod__search_engine_ragflow_recall_tool"
    await engine.check_permission(SimpleNamespace(name=name, is_external_tool=False), {"question": "核验"})
    values = [result(), result(), result(1)] + ([{"error_code": "MISSING_SCOPE"}] if late_error else [])

    async def execute(**kwargs):
        response = CallToolResult(content=[TextContent(type="text", text=json.dumps(v)) for v in values])
        yield ToolResponse(id="call", content=_blocks_from_value(response), state=ToolResultState.SUCCESS)

    responses = [item async for item in middleware.on_acting(SimpleNamespace(),
        {"tool_call": ToolCallBlock(id="call", name=name, input=json.dumps({"question": "核验"}))}, execute)]
    event = client.events[-1]
    if late_error:
        assert event[1:3] == ("failed", "MISSING_SCOPE")
        assert "knowledge_sources" not in event[3]
        assert responses[-1].state == ToolResultState.ERROR
        assert "第0条正文" not in responses[-1].content[0].text
    else:
        assert event[1:3] == ("completed", "")
        assert len(event[3]["knowledge_sources"]) == 2
        assert "第0条正文" in responses[-1].content[0].text
        assert "第1条正文" in responses[-1].content[0].text
        assert "ks_" + f"{2:032x}" in responses[-1].content[0].text
