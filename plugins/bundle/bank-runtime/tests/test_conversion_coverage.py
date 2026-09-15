import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from types import SimpleNamespace

import pytest
from agentscope.message import TextBlock, ToolCallBlock, ToolResultState
from agentscope.model import ChatResponse
from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.tool import ToolResponse
from bank_runtime.artifact_tools import DocumentReadIncompleteError
from bank_runtime.gateway.middleware import BankRuntimeGatewayMiddleware, GatewayPermissionEngine
from bank_runtime.presentation import artifact_model_result


REPORT = {"schema_version": "1.0", "coverage": "partial", "editable": False,
          "warnings": ["object_unreadable"],
          "objects": [{"index": 1, "kind": "visio", "status": "unreadable"}]}
PAYLOAD = {"source_type": "session_file", "source_id": "source", "target_format": "pdf", "purpose": "read"}


class Client:
    def __init__(self, report=REPORT):
        self.executions = []
        self.reports = []
        self.envelope = {"status": "success", "result": {"artifact_status": "succeeded",
            "generated_file_ids": ["converted"], "purpose": "read", "conversion_report": report}}

    async def report_guard(self, *args): pass
    async def report_result(self, *args): self.reports.append(args)
    async def preflight(self, *args, **kwargs): return {"tool_call_id": "c"}
    async def execute_runtime_tool(self, *args):
        self.executions.append(args)
        return self.envelope


async def invoke(middleware, name, payload, response=None):
    middleware.prepare(name, payload, {"tool_call_id": "c"})
    async def handler():
        yield ToolResponse(id="c", state=ToolResultState.SUCCESS, content=[TextBlock(text=json.dumps(response))])
    return [item async for item in middleware.on_acting(None, {"tool_call": ToolCallBlock(
        id="c", name=name, input=json.dumps(payload))}, handler)]


async def read_converted(middleware, chunked=False):
    item = {"file_id": "converted", "status": "completed", "content_mode": "inline", "markdown": "正文"}
    if chunked:
        item.update(content_mode="chunked", document_ref="ref", chunk_count=2)
    await invoke(middleware, "MinerU__parse_documents", {"documents": [{"file_id": "converted"}]},
                 {"status": "completed", "items": [item]})


@pytest.mark.asyncio
async def test_conversion_requires_read_and_partial_answer_always_has_trusted_scope():
    middleware = BankRuntimeGatewayMiddleware(Client())
    await invoke(middleware, "artifact_convert", PAYLOAD)
    assert "parse:converted" in middleware.unresolved_file_operations
    async def early(**kwargs): return ChatResponse(id="a", content=[TextBlock(text="总结")], is_last=True)
    with pytest.raises(DocumentReadIncompleteError):
        await middleware.on_model_call(None, {}, early)
    middleware._read_guard_failed = False
    await read_converted(middleware)
    seen = []
    async def model(**kwargs):
        seen.extend(kwargs["messages"])
        return ChatResponse(id="a", content=[TextBlock(text="正文内容如下。")], is_last=True)
    result = await middleware.on_model_call(None, {"messages": []}, model)
    assert any("部分" in str(message.content) for message in seen)
    assert "部分" in result.content[0].text
    assert "未读取" in result.content[0].text
    assert middleware.conversion_coverage.partial
    middleware._check_file_completion()


@pytest.mark.asyncio
async def test_partial_conversion_does_not_disable_pagination_or_generated_report_guard():
    middleware = BankRuntimeGatewayMiddleware(Client())
    await invoke(middleware, "artifact_convert", PAYLOAD)
    await read_converted(middleware, chunked=True)
    async def model(**kwargs): return ChatResponse(id="a", content=[TextBlock(text="总结")], is_last=True)
    with pytest.raises(DocumentReadIncompleteError):
        await middleware.on_model_call(None, {}, model)
    middleware._read_guard_failed = False
    await invoke(middleware, "MinerU__read_document_chunks", {"document_ref": "ref"},
                 {"document_ref": "ref", "chunks": [{"index": 0, "text": "a"}, {"index": 1, "text": "b"}],
                  "has_more": False, "next_cursor": None})
    assert not middleware.document_reads.pending
    with pytest.raises(DocumentReadIncompleteError):
        await invoke(middleware, "artifact_generate", {"artifact_type": "docx", "title": "全量报告"})
    assert len(middleware.client.executions) == 1


@pytest.mark.asyncio
async def test_known_conversion_failure_blocks_identical_execution_and_preflight():
    client = Client()
    client.envelope = {"status": "failed", "error_code": "ARTIFACT_VALIDATION_FAILED",
                       "result": {"reason": "office_active_content", "message": "SECRET"}}
    middleware = BankRuntimeGatewayMiddleware(client)
    result = await invoke(middleware, "artifact_convert", PAYLOAD)
    assert "活动内容" in result[0].content[0].text
    assert "SECRET" not in result[0].content[0].text
    class Delegate:
        async def check_permission(self, *args): return PermissionDecision(behavior=PermissionBehavior.ALLOW)
    decision = await GatewayPermissionEngine(Delegate(), middleware).check_permission(
        SimpleNamespace(name="artifact_convert"), PAYLOAD)
    assert decision.behavior == PermissionBehavior.DENY
    await invoke(middleware, "artifact_convert", PAYLOAD)
    assert len(client.executions) == 1


def test_model_result_exposes_validated_scope_but_internal_pdf_is_not_a_delivery():
    result = artifact_model_result(Client().envelope)
    assert result["result"]["conversion_report"] == REPORT
    assert "下载" not in result["presentation"]["message"]
    assert "部分" in result["presentation"]["message"]


@pytest.mark.parametrize("report", [dict(REPORT, coverage="complete"), dict(REPORT, objects=[{"index": True, "kind": "visio", "status": "unreadable"}]), dict(REPORT, warnings=["SECRET"])])
def test_invalid_reports_do_not_enter_model_context(report):
    from bank_runtime.conversion_reports import validate_conversion_report
    assert validate_conversion_report(report) is None


@pytest.mark.asyncio
async def test_partial_stream_snapshots_all_retain_scope_prefix():
    middleware = BankRuntimeGatewayMiddleware(Client())
    await invoke(middleware, "artifact_convert", PAYLOAD)
    await read_converted(middleware)
    async def stream():
        yield ChatResponse(id="a", content=[TextBlock(text="正文")], is_last=False)
        yield ChatResponse(id="a", content=[TextBlock(text="正文摘要。")], is_last=True)
    async def model(**kwargs): return stream()
    response = await middleware.on_model_call(None, {}, model)
    chunks = [chunk async for chunk in response]
    assert len(chunks) == 2
    assert all("未读取" in chunk.content[0].text for chunk in chunks)
    assert chunks[-1].content[-1].text == "正文摘要。"


@pytest.mark.asyncio
async def test_derivative_parse_cannot_erase_original_authorization_denial():
    middleware = BankRuntimeGatewayMiddleware(Client())
    await invoke(middleware, "MinerU__parse_documents", {"documents": [{"file_id": "source"}]},
                 {"status": "failed", "items": [{"file_id": "source", "status": "failed", "error_code": "FILE_ACCESS_DENIED"}]})
    await invoke(middleware, "artifact_convert", PAYLOAD)
    await read_converted(middleware)
    assert middleware.document_reads.failures["parse:source"] == "FILE_ACCESS_DENIED"
    async def model(**kwargs): return ChatResponse(id="a", content=[TextBlock(text="摘要")], is_last=True)
    with pytest.raises(DocumentReadIncompleteError):
        await middleware.on_model_call(None, {}, model)


@pytest.mark.asyncio
async def test_transport_conversion_failure_retains_safe_reason_and_stops_retry():
    from bank_runtime.gateway.client import GatewayError, _response_error
    client = Client()
    async def failing(*args):
        client.executions.append(args)
        raise _response_error({"detail": {"code": "ARTIFACT_VALIDATION_FAILED", "message": "SECRET",
              "details": {"reason": "office_package_invalid", "message": "SECRET"}}}, "fallback")
    client.execute_runtime_tool = failing
    middleware = BankRuntimeGatewayMiddleware(client)
    with pytest.raises(GatewayError) as error:
        await invoke(middleware, "artifact_convert", PAYLOAD)
    assert "SECRET" not in str(error.value)
    assert "结构损坏" in str(error.value)
    await invoke(middleware, "artifact_convert", PAYLOAD)
    assert len(client.executions) == 1


def test_report_limits_and_editability_are_strict():
    from bank_runtime.conversion_reports import validate_conversion_report
    assert validate_conversion_report(dict(REPORT, editable=True)) is None
    assert validate_conversion_report(dict(REPORT, objects=[{"index": 201, "kind": "object", "status": "unreadable"}])) is None
    complete = dict(REPORT, coverage="complete", editable=False, warnings=["object_static"],
                    objects=[{"index": 1, "kind": "chart", "status": "static"}])
    assert validate_conversion_report(complete) == complete


@pytest.mark.asyncio
async def test_unknown_reason_does_not_disable_retry_or_expose_text():
    client = Client()
    client.envelope = {"status": "failed", "error_code": "ARTIFACT_RENDER_FAILED", "result": {"reason": "SECRET"}}
    middleware = BankRuntimeGatewayMiddleware(client)
    for _ in range(2):
        response = await invoke(middleware, "artifact_convert", PAYLOAD)
        assert "SECRET" not in response[0].content[0].text
    assert len(client.executions) == 2


def test_convert_schema_exposes_read_purpose_without_pdf_confirmation():
    import inspect
    from agentscope.tool import FunctionTool
    from bank_runtime.artifact_tools import artifact_convert
    assert inspect.signature(artifact_convert).parameters["purpose"].default == "delivery"
    assert "purpose" in FunctionTool(artifact_convert).input_schema["properties"]


@pytest.mark.asyncio
async def test_partial_model_cannot_stream_prose_or_request_a_generated_full_report():
    middleware = BankRuntimeGatewayMiddleware(Client())
    await invoke(middleware, "artifact_convert", PAYLOAD)
    await read_converted(middleware)
    async def model(**kwargs):
        return ChatResponse(id="a", content=[TextBlock(text="全部读完，生成报告"), ToolCallBlock(
            id="g", name="artifact_generate", input='{"artifact_type":"docx","title":"全量报告"}')], is_last=True)
    with pytest.raises(DocumentReadIncompleteError) as error:
        await middleware.on_model_call(None, {}, model)
    assert error.value.error_code == "DOCUMENT_CONVERSION_PARTIAL"


@pytest.mark.asyncio
async def test_partial_delivery_is_labeled_without_claiming_document_was_read():
    from bank_runtime.artifact_tools import ArtifactDeliveryIntent
    client = Client()
    client.envelope["result"]["purpose"] = "delivery"
    middleware = BankRuntimeGatewayMiddleware(client, artifact_intent=ArtifactDeliveryIntent("convert", "docx"))
    await invoke(middleware, "artifact_convert", dict(PAYLOAD, purpose="delivery", target_format="docx"))
    assert not middleware.conversion_coverage.partial_read
    assert "parse:converted" not in middleware.unresolved_file_operations
    async def model(**kwargs): return ChatResponse(id="a", content=[TextBlock(text="转换结果见文件卡片。")], is_last=True)
    result = await middleware.on_model_call(None, {}, model)
    assert "仅保留" in result.content[0].text
    assert "已读取" not in result.content[0].text


@pytest.mark.asyncio
async def test_terminal_conversion_reason_is_fixed_for_runtime_translator():
    from bank_runtime.artifact_tools import OfficeConversionFailureError
    client = Client()
    client.envelope = {"status": "failed", "error_code": "ARTIFACT_VALIDATION_FAILED", "result": {"reason": "office_active_content"}}
    middleware = BankRuntimeGatewayMiddleware(client)
    await invoke(middleware, "artifact_convert", PAYLOAD)
    async def model(**kwargs): pytest.fail("known permanent failure must not prompt further model retries")
    with pytest.raises(OfficeConversionFailureError) as error:
        await middleware.on_model_call(None, {}, model)
    assert error.value.message == "OFFICE_CONVERSION|office_active_content"
    with pytest.raises(OfficeConversionFailureError):
        middleware._check_file_completion()


@pytest.mark.parametrize("objects,warnings", [
    ([{"index": 1, "kind": "attachment", "status": "extracted"}], []),
    ([], ["attachment_extracted"]),
])
def test_extracted_text_cannot_claim_complete_attachment_coverage(objects, warnings):
    from bank_runtime.conversion_reports import validate_conversion_report
    assert validate_conversion_report(dict(REPORT, coverage="complete", objects=objects, warnings=warnings)) is None


def test_same_file_new_complete_report_cannot_erase_prior_partial_evidence():
    from bank_runtime.conversion_reports import ConversionCoverage
    coverage = ConversionCoverage()
    coverage.observe(["same"], REPORT, requires_read=True)
    coverage.observe(["same"], dict(REPORT, coverage="complete", editable=True, objects=[], warnings=[]), requires_read=False)
    assert coverage.partial_read
    assert coverage.reports["same"]["coverage"] == "partial"
    assert coverage.reports["same"]["editable"] is False
    assert coverage.reports["same"]["warnings"] == ["object_unreadable"]
    assert coverage.reports["same"]["objects"] == REPORT["objects"]


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["docx", "pdf"])
async def test_static_only_markdown_does_not_prove_chart_visual_understanding(target):
    report = dict(REPORT, coverage="complete", warnings=["object_static"],
                  objects=[{"index": 1, "kind": "visio", "status": "static"}])
    middleware = BankRuntimeGatewayMiddleware(Client(report))
    await invoke(middleware, "artifact_convert", dict(PAYLOAD, target_format=target))
    await read_converted(middleware)
    async def model(**kwargs): return ChatResponse(id="a", content=[TextBlock(text="文档正文摘要。")], is_last=True)
    result = await middleware.on_model_call(None, {}, model)
    assert "图形" in result.content[0].text and "未核验" in result.content[0].text
    assert middleware.conversion_coverage.reports["converted"]["coverage"] == "complete"
    with pytest.raises(DocumentReadIncompleteError):
        await invoke(middleware, "artifact_generate", {"artifact_type": "docx", "title": "完整流程图分析"})


def test_partial_conversion_failure_survives_public_event_projection():
    from bank_runtime.events import CompactEventProjector

    events = CompactEventProjector("task-partial").project({
        "object": "response", "status": "failed",
        "error": {"code": "DOCUMENT_CONVERSION_PARTIAL", "message": "private source content"},
    })

    assert events[-1]["event"] == "answer.failed"
    assert events[-1]["error_code"] == "DOCUMENT_CONVERSION_PARTIAL"
    assert "private source content" not in str(events)
