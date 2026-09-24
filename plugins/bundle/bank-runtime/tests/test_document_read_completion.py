import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from types import SimpleNamespace
import json

import pytest
from agentscope.message import TextBlock, ToolCallBlock, ToolResultState
from agentscope.model import ChatResponse
from agentscope.tool import ToolResponse
from bank_runtime.gateway.middleware import BankRuntimeGatewayMiddleware
from bank_runtime.artifact_tools import FileOperationsIncompleteError


class Client:
    config = SimpleNamespace(task_id="task_001")
    def __init__(self):
        self.reports = []
        self.executions = []

    async def report_guard(self, *args): pass

    async def report_result(self, *args): self.reports.append(args)

    async def execute_runtime_tool(self, *args):
        self.executions.append(args)
        return {"status": "success", "result": {"artifact_status": "succeeded", "generated_file_ids": ["report"]}}


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["MinerU__read_range", "MinerU__aggregate"])
async def test_unobserved_reference_denial_is_not_reported_as_incomplete_read(tool_name):
    from bank_runtime.gateway.client import GatewayError
    middleware = BankRuntimeGatewayMiddleware(Client())
    payload = {"document_ref": "previous-task-reference"}
    if tool_name.endswith("aggregate"):
        payload["ops"] = [{"sheet": "台账", "metrics": [{"column": "金额", "fn": "sum"}]}]
    middleware.prepare(tool_name, payload, {"tool_call_id": "denied-call"})
    async def handler():
        pytest.fail("Unobserved reference must never reach the document service")
        yield
    with pytest.raises(GatewayError):
        _ = [item async for item in middleware.on_acting(None, {"tool_call": ToolCallBlock(
            id="denied-call", name=tool_name, input=json.dumps(payload))}, handler)]
    assert middleware.client.reports[-1][-1] == "FILE_ACCESS_DENIED"
    assert set(middleware.document_reads.failures.values()) == {"FILE_ACCESS_DENIED"}
    # Successful work on a different authorized reference cannot erase this denial.
    await parse(middleware, count=2)
    await read(middleware, 0, 2, total=2)
    assert middleware.document_reads.pending
    assert middleware.document_reads.error_code == "ARTIFACT_OUTPUT_MISSING"


async def invoke(middleware, name, payload, result, state=ToolResultState.SUCCESS):
    middleware.prepare(name, payload, {"tool_call_id": "call"})
    async def handler():
        yield ToolResponse(id="call", state=state, content=[TextBlock(text=json.dumps(result))])
    return [item async for item in middleware.on_acting(None, {"tool_call": ToolCallBlock(
        id="call", name=name, input=json.dumps(payload))}, handler)]


async def parse(middleware, ref="doc", count=24):
    await invoke(middleware, "MinerU__parse_documents", {"documents": [{"file_id": "f1"}]}, {
        "status": "completed", "items": [{"file_id": "f1", "status": "completed",
        "content_mode": "chunked", "document_ref": ref, "chunk_count": count}]})


def page(start, stop, total=24, ref="doc"):
    return {"document_ref": ref, "chunks": [{"index": i, "text": f"row {i}"} for i in range(start, stop)],
            "has_more": stop < total, "next_cursor": str(stop) if stop < total else None}


async def read(middleware, start, stop, total=24, ref="doc"):
    return await invoke(middleware, "MinerU__read_document_chunks", {
        "document_ref": ref, "cursor": str(start) if start else None, "limit": stop-start}, page(start, stop, total, ref))


@pytest.mark.asyncio
async def test_partial_read_cannot_stream_final_totals_or_publish_report():
    middleware = BankRuntimeGatewayMiddleware(Client())
    await parse(middleware)
    for start in range(0, 18, 2):
        await read(middleware, start, start+2)
    async def model(**kwargs):
        return ChatResponse(id="a", content=[TextBlock(text="已全量统计，总量123")], is_last=True)
    with pytest.raises(FileOperationsIncompleteError) as error:
        await middleware.on_model_call(None, {}, model)
    assert error.value.error_code == "DOCUMENT_READ_INCOMPLETE"
    with pytest.raises(FileOperationsIncompleteError) as blocked:
        await invoke(middleware, "artifact_generate", {"artifact_type": "markdown", "title": "报告"}, {})
    assert blocked.value.error_code == "DOCUMENT_READ_INCOMPLETE"
    assert middleware.client.executions == []


@pytest.mark.asyncio
async def test_all_pages_with_retry_complete_and_do_not_double_count():
    middleware = BankRuntimeGatewayMiddleware(Client())
    await parse(middleware)
    for start in range(0, 24, 2):
        await read(middleware, start, start+2)
        await read(middleware, start, start+2)
    assert not middleware.document_reads.pending
    assert middleware.document_reads.coverage("doc") == (24, 24)
    response = ChatResponse(id="a", content=[TextBlock(text="完整结果")], is_last=True)
    async def model(**kwargs): return response
    assert await middleware.on_model_call(None, {}, model) is response


@pytest.mark.asyncio
async def test_last_page_alone_or_wrong_document_does_not_prove_complete():
    middleware = BankRuntimeGatewayMiddleware(Client())
    await parse(middleware)
    await read(middleware, 22, 24)
    assert middleware.document_reads.pending
    await invoke(middleware, "MinerU__read_document_chunks", {"document_ref": "doc"}, page(0, 24, ref="other"))
    assert middleware.document_reads.pending


@pytest.mark.asyncio
async def test_retry_failure_recovers_only_same_page_and_reports_safe_reason():
    middleware = BankRuntimeGatewayMiddleware(Client())
    await parse(middleware, count=4)
    await read(middleware, 0, 2, total=4)
    await invoke(middleware, "MinerU__read_document_chunks", {"document_ref": "doc", "cursor": "2"},
                 {"status": "failed", "error_code": "DOCUMENT_REF_EXPIRED"})
    assert middleware.client.reports[-1][1] == "failed"
    assert middleware.client.reports[-1][-1] == "DOCUMENT_REF_EXPIRED"
    await read(middleware, 2, 4, total=4)
    assert not middleware.document_reads.pending


@pytest.mark.asyncio
async def test_no_progress_is_bounded_and_does_not_call_model_again():
    middleware = BankRuntimeGatewayMiddleware(Client())
    await parse(middleware)
    for _ in range(4):
        await read(middleware, 0, 2)
    async def model(**kwargs): pytest.fail("no further model retry")
    with pytest.raises(FileOperationsIncompleteError) as error:
        await middleware.on_model_call(None, {}, model)
    assert error.value.error_code == "DOCUMENT_READ_NO_PROGRESS"


@pytest.mark.asyncio
async def test_pending_read_allows_continuation_tools_but_not_ungrounded_prose():
    middleware = BankRuntimeGatewayMiddleware(Client())
    await parse(middleware)
    async def model(**kwargs):
        return ChatResponse(id="a", content=[TextBlock(text="我已读全"), ToolCallBlock(
            id="next", name="MinerU__read_document_chunks", input='{"document_ref":"doc"}')], is_last=True)
    response = await middleware.on_model_call(None, {}, model)
    assert any(isinstance(block, ToolCallBlock) for block in response.content)
    assert not any(isinstance(block, TextBlock) for block in response.content)


@pytest.mark.asyncio
async def test_reparse_same_file_supersedes_old_handle_but_not_other_files():
    middleware = BankRuntimeGatewayMiddleware(Client())
    await parse(middleware, ref="old")
    await parse(middleware, ref="new", count=2)
    await read(middleware, 0, 2, total=2, ref="new")
    assert not middleware.document_reads.pending


@pytest.mark.asyncio
async def test_chunked_parse_without_any_read_fails_at_reply_completion():
    middleware = BankRuntimeGatewayMiddleware(Client())
    async def reply(**kwargs):
        await parse(middleware)
        yield "candidate"
    with pytest.raises(FileOperationsIncompleteError):
        _ = [item async for item in middleware.on_reply(None, {}, reply)]


@pytest.mark.asyncio
async def test_truncated_text_preparation_stops_model_and_writer(tmp_path):
    from types import SimpleNamespace
    from bank_runtime.sandbox.processor import AttachmentProcessor
    from bank_runtime.sandbox.tools import set_sandbox_tool_state, reset_sandbox_tool_state
    from bank_runtime.sandbox.cache import PreparedSandboxFile
    path = tmp_path / "table.csv"
    path.write_text("用户,token\n甲,10\n乙,20\n")
    processor = AttachmentProcessor(per_file_chars=5)
    processor.process([PreparedSandboxFile(file_id="f", local_path=path, content_type="text/csv",
        size_bytes=path.stat().st_size, original_name="table.csv", expires_at="")])
    token = set_sandbox_tool_state(SimpleNamespace(processor=processor))
    try:
        middleware = BankRuntimeGatewayMiddleware(Client())
        async def model(**kwargs): pytest.fail("truncated source cannot yield a full-data answer")
        with pytest.raises(FileOperationsIncompleteError) as error:
            await middleware.on_model_call(None, {}, model)
        assert error.value.error_code == "DOCUMENT_TEXT_TRUNCATED"
    finally:
        reset_sandbox_tool_state(token)


@pytest.mark.asyncio
async def test_streaming_incomplete_round_withholds_every_prose_chunk():
    middleware = BankRuntimeGatewayMiddleware(Client())
    await parse(middleware)
    async def stream():
        yield ChatResponse(id="a", content=[TextBlock(text="虚假的全量统计")], is_last=False)
        yield ChatResponse(id="a", content=[TextBlock(text="虚假的全量统计已完成")], is_last=True)
    async def model(**kwargs): return stream()
    with pytest.raises(FileOperationsIncompleteError):
        await middleware.on_model_call(None, {}, model)


@pytest.mark.asyncio
async def test_partial_batch_keeps_reason_but_recovery_cannot_erase_another_file():
    middleware = BankRuntimeGatewayMiddleware(Client())
    await invoke(middleware, "MinerU__parse_documents", {"documents": [{"file_id": "f1"}, {"file_id": "f2"}]}, {
        "status": "partial", "items": [
            {"file_id": "f1", "status": "completed", "content_mode": "inline", "markdown": "可用"},
            {"file_id": "f2", "status": "failed", "error_code": "DOCUMENT_RESULT_TOO_LARGE"}]})
    assert middleware.document_reads.error_code == "DOCUMENT_RESULT_TOO_LARGE"
    await parse(middleware, count=2)
    await read(middleware, 0, 2, total=2)
    assert middleware.document_reads.pending


@pytest.mark.asyncio
async def test_ambiguous_submit_stops_before_retry_and_is_not_a_transient_read():
    middleware = BankRuntimeGatewayMiddleware(Client())
    await invoke(middleware, "MinerU__parse_documents", {"documents": [{"file_id": "f1"}]},
                 {"status": "failed", "items": [], "error_code": "MINERU_SUBMIT_AMBIGUOUS"})
    async def model(**kwargs): pytest.fail("do not resubmit an unknown remote job")
    with pytest.raises(FileOperationsIncompleteError) as error:
        await middleware.on_model_call(None, {}, model)
    assert error.value.error_code == "MINERU_SUBMIT_AMBIGUOUS"


@pytest.mark.asyncio
async def test_pending_read_cannot_bypass_report_guard_through_physical_tool():
    middleware = BankRuntimeGatewayMiddleware(Client())
    await parse(middleware)
    with pytest.raises(FileOperationsIncompleteError):
        await invoke(middleware, "execute_shell_command", {"command": "echo fake > report.md"}, {})
    assert middleware.client.reports[-1][1] == "failed"
    assert middleware.client.reports[-1][-1] == "DOCUMENT_READ_INCOMPLETE"


@pytest.mark.asyncio
async def test_scope_denial_is_audited_and_never_becomes_retryable_read_error():
    middleware = BankRuntimeGatewayMiddleware(Client())
    await invoke(middleware, "MinerU__parse_documents", {"documents": [{"file_id": "f1"}]},
                 {"status": "failed", "items": [], "error_code": "FILE_ACCESS_DENIED"})
    assert middleware.client.reports[-1][-1] == "FILE_ACCESS_DENIED"
    assert middleware.document_reads.error_code == "ARTIFACT_OUTPUT_MISSING"


@pytest.mark.asyncio
async def test_unknown_write_wins_over_retryable_read_failure():
    middleware = BankRuntimeGatewayMiddleware(Client())
    middleware.unresolved_file_operations.add("artifact:docx:previous-output")
    await parse(middleware)
    async def model(**kwargs):
        return ChatResponse(id="a", content=[TextBlock(text="不完整答复")], is_last=True)
    with pytest.raises(FileOperationsIncompleteError) as error:
        await middleware.on_model_call(None, {}, model)
    assert error.value.error_code == "ARTIFACT_OUTPUT_MISSING"


@pytest.mark.asyncio
@pytest.mark.parametrize("item", [
    {"file_id": [], "status": "completed"},
    {"file_id": "f1", "status": "completed", "content_mode": "unknown"},
    {"file_id": "f1", "status": "completed", "content_mode": "inline", "markdown": None},
])
async def test_malformed_parse_metadata_remains_readable_failure(item):
    middleware = BankRuntimeGatewayMiddleware(Client())
    await invoke(middleware, "MinerU__parse_documents", {"documents": [{"file_id": "f1"}]},
                 {"status": "completed", "items": [item]})
    assert middleware.document_reads.pending


@pytest.mark.asyncio
async def test_argument_retry_stops_with_original_reason():
    middleware = BankRuntimeGatewayMiddleware(Client())
    await parse(middleware)
    for _ in range(2):
        await invoke(middleware, 'MinerU__aggregate', {'document_ref':'doc','ops':[{}]},
                     {'status':'failed','error_code':'DOCUMENT_ARGUMENT_INVALID'})
    async def model(**kwargs): pytest.fail('argument correction budget exhausted')
    with pytest.raises(FileOperationsIncompleteError) as error:
        await middleware.on_model_call(None, {}, model)
    assert error.value.error_code == 'DOCUMENT_ARGUMENT_INVALID'


@pytest.mark.asyncio
async def test_native_metadata_does_not_escape_yield_and_can_close_in_other_task():
    import asyncio
    from qwenpaw.drivers.mcp_context import current_mcp_metadata
    from bank_runtime.gateway.document_access import consume_document_call, current_document_task
    middleware = BankRuntimeGatewayMiddleware(Client())
    payload = {'documents':[{'file_id':'f1','file_ref':'r1'}]}
    closed = []
    async def handler():
        metadata = current_mcp_metadata()
        assert metadata
        with consume_document_call(metadata, 'parse_documents', payload):
            assert current_document_task() == 'task_001'
        try:
            yield 'first'
        finally:
            closed.append(True)
    stream = middleware._authorized_native_call('MinerU__parse_documents', payload, handler)
    assert await anext(stream) == 'first'
    assert current_mcp_metadata() is None
    await asyncio.create_task(stream.aclose())
    assert closed == [True]
    assert current_mcp_metadata() is None


@pytest.mark.asyncio
async def test_metadata_revoked_on_cancel_and_concurrent_calls_stay_isolated():
    import asyncio
    from qwenpaw.drivers.mcp_context import current_mcp_metadata
    from bank_runtime.gateway.document_access import consume_document_call, current_document_task, DocumentAccessError
    async def run(task_id):
        client = Client()
        client.config = SimpleNamespace(task_id=task_id)
        middleware = BankRuntimeGatewayMiddleware(client)
        payload = {'documents':[{'file_id':task_id,'file_ref':'ref'}]}
        seen = []
        entered = asyncio.Event()
        closed = []
        async def handler():
            seen.append(current_mcp_metadata())
            entered.set()
            try:
                await asyncio.Event().wait()
                yield 'unreachable'
            finally:
                closed.append(True)
        stream = middleware._authorized_native_call('MinerU__parse_documents', payload, handler)
        pending = asyncio.create_task(anext(stream))
        await entered.wait()
        assert current_mcp_metadata() is None
        with consume_document_call(seen[0], 'parse_documents', payload):
            assert current_document_task() == task_id
        with pytest.raises(DocumentAccessError):
            with consume_document_call(seen[0], 'parse_documents', payload): pass
        pending.cancel()
        with pytest.raises(asyncio.CancelledError): await pending
        assert closed == [True]
        assert current_mcp_metadata() is None
        return seen[0]
    first, second = await asyncio.gather(run('task_a'), run('task_b'))
    assert first != second


@pytest.mark.asyncio
async def test_outer_acting_close_revokes_unconsumed_grant():
    import asyncio
    from qwenpaw.drivers.mcp_context import current_mcp_metadata
    from bank_runtime.gateway.document_access import consume_document_call, DocumentAccessError
    middleware = BankRuntimeGatewayMiddleware(Client())
    payload = {'documents':[{'file_id':'f','file_ref':'r'}]}
    name = 'MinerU__parse_documents'
    middleware.prepare(name, payload, {'tool_call_id':'call'})
    seen, closed = [], []
    async def handler():
        seen.append(current_mcp_metadata())
        try:
            yield ToolResponse(id='call', state=ToolResultState.SUCCESS, content=[TextBlock(text='{}')])
        finally:
            closed.append(True)
    stream = middleware.on_acting(None, {'tool_call':ToolCallBlock(id='call',name=name,input=json.dumps(payload))}, handler)
    await anext(stream)
    assert current_mcp_metadata() is None
    await asyncio.create_task(stream.aclose())
    assert closed == [True]
    with pytest.raises(DocumentAccessError):
        with consume_document_call(seen[0], 'parse_documents', payload): pass


@pytest.mark.asyncio
async def test_independently_read_document_can_deliver_scoped_text_with_trusted_gap_evidence():
    from bank_runtime.delivery_state import begin_delivery_state, end_delivery_state
    middleware = BankRuntimeGatewayMiddleware(Client())
    await invoke(middleware, 'MinerU__parse_documents', {'documents':[{'file_id':'f1'},{'file_id':'f2'}]},
                 {'items':[{'file_id':'f1','status':'completed','content_mode':'inline','document_ref':'doc1','markdown':'已核实正文'},
                           {'file_id':'f2','status':'failed','error_code':'DOCUMENT_PARSE_FAILED'}]})
    state, token = begin_delivery_state('task_001')
    try:
        async def model(**kwargs):
            return ChatResponse(id='partial', content=[TextBlock(text='第一份材料说明了审批流程。')], is_last=True)
        response = await middleware.on_model_call(None, {}, model)
        assert response.content[0].text == '第一份材料说明了审批流程。'
        assert state.analysis['delivery'] == 'partial'
        assert state.analysis['gaps'] == [{'kind':'document','file_id':'f2','impact':'scope_unread'}]
        middleware._check_file_completion()
    finally:
        end_delivery_state(token)


@pytest.mark.asyncio
async def test_no_cross_request_partial_evidence_or_disclaimer_licensed_total():
    from bank_runtime.delivery_state import begin_delivery_state, end_delivery_state
    middleware = BankRuntimeGatewayMiddleware(Client())
    await parse(middleware, ref='good', count=2)
    await read(middleware,0,2,total=2,ref='good')
    await invoke(middleware, 'MinerU__parse_documents', {'documents':[{'file_id':'f2'}]},
                 {'items':[{'file_id':'f2','status':'failed','error_code':'DOCUMENT_PARSE_FAILED'}]})
    state, token = begin_delivery_state('different-task')
    try:
        async def model(**kwargs):
            return ChatResponse(id='partial', content=[TextBlock(text='第一份材料说明了审批流程。')], is_last=True)
        with pytest.raises(FileOperationsIncompleteError):
            await middleware.on_model_call(None, {}, model)
        assert state.analysis == {}
    finally:
        end_delivery_state(token)

@pytest.mark.asyncio
async def test_partial_report_uses_same_admitted_payload_and_marks_file_and_body():
    from bank_runtime.delivery_state import begin_delivery_state, end_delivery_state
    from bank_runtime.artifact_tools import ArtifactDeliveryIntent
    middleware = BankRuntimeGatewayMiddleware(Client(), artifact_intent=ArtifactDeliveryIntent('generate', 'docx', ('f1','f2')))
    await invoke(middleware, 'MinerU__parse_documents', {'documents':[{'file_id':'f1'},{'file_id':'f2'}]},
        {'items':[{'file_id':'f1','status':'completed','content_mode':'inline','document_ref':'good','markdown':'流程正文'},
                  {'file_id':'f2','status':'failed','error_code':'DOCUMENT_PARSE_FAILED'}]})
    state, token = begin_delivery_state('task_001')
    try:
        payload = {'artifact_type':'docx','title':'材料分析','output_name':'材料分析.docx','content':{'paragraphs':['第一份材料的流程包含审批与复核。']}}
        normalized = middleware.document_input('artifact_generate', payload)
        assert normalized['output_name'] == '材料分析（部分稿）.docx'
        assert '第2份材料' in normalized['content']['paragraphs'][0]
        middleware.prepare('artifact_generate', normalized, {'tool_call_id':'artifact'})
        async def unused():
            raise AssertionError('Must execute through Runtime')
            yield
        results = [item async for item in middleware.on_acting(None, {'tool_call':ToolCallBlock(id='artifact',name='artifact_generate',input=json.dumps(payload))}, unused)]
        assert len(results) == 1
        assert middleware.client.executions[-1][2] == normalized
        assert state.analysis['delivery'] == 'partial'
        middleware._check_file_completion()
    finally:
        end_delivery_state(token)

@pytest.mark.asyncio
async def test_explicit_complete_report_and_disclaimer_followed_by_total_stay_blocked():
    from bank_runtime.delivery_state import begin_delivery_state, end_delivery_state
    from bank_runtime.artifact_tools import ArtifactDeliveryIntent
    middleware = BankRuntimeGatewayMiddleware(Client(), artifact_intent=ArtifactDeliveryIntent('generate','docx',('f1','f2'), input_scope='complete'))
    await invoke(middleware, 'MinerU__parse_documents', {'documents':[{'file_id':'f1'},{'file_id':'f2'}]},
        {'items':[{'file_id':'f1','status':'completed','content_mode':'inline','document_ref':'good','markdown':'流程正文'},
                  {'file_id':'f2','status':'failed','error_code':'DOCUMENT_PARSE_FAILED'}]})
    state, token = begin_delivery_state('task_001')
    try:
        payload = {'artifact_type':'docx','content':{'paragraphs':['第一份材料的审批流程。']}}
        assert middleware.document_input('artifact_generate', payload) == payload
        assert not middleware.document_reads.permits_scoped_answer('部分文件尚未读取，但是所有材料的总计为100。')
        async def model(**kwargs):
            return ChatResponse(id='partial',content=[TextBlock(text='第一份材料的审批流程。')],is_last=True)
        with pytest.raises(FileOperationsIncompleteError):
            await middleware.on_model_call(None, {}, model)
        assert state.analysis == {}
    finally:
        end_delivery_state(token)

@pytest.mark.asyncio
async def test_partial_report_conversion_only_accepts_this_turn_verified_output():
    from bank_runtime.delivery_state import begin_delivery_state, end_delivery_state
    from bank_runtime.artifact_tools import ArtifactDeliveryIntent
    middleware=BankRuntimeGatewayMiddleware(Client(),artifact_intent=ArtifactDeliveryIntent('generate','docx',('f1','f2')))
    state,token=begin_delivery_state('task_001')
    try:
        payload={'source_generated_file_id':'other','target_format':'pdf','output_name':'报告.pdf'}
        assert middleware._partial_report('artifact_convert',payload) is None
        middleware._partial_generated_ids.add('verified-this-turn')
        payload['source_generated_file_id']='verified-this-turn'
        normalized=middleware._partial_report('artifact_convert',payload)
        assert normalized['output_name']=='报告（部分稿）.pdf'
        assert 'explicit_pdf_request' not in normalized
        assert middleware._partial_report('artifact_convert',{**payload,'purpose':'read'}) is None
    finally:
        end_delivery_state(token)

@pytest.mark.asyncio
async def test_delivery_metadata_is_producer_local_and_raw_event_cannot_supply_it():
    from bank_runtime.delivery_state import current_delivery_state
    from bank_runtime.events import project_sse_stream
    evidence={'version':'reading-delivery-1','delivery':'partial','gaps':[{'kind':'document','file_id':'f2','impact':'scope_unread'}]}
    async def source(trusted):
        assert current_delivery_state().task_id == 'task_001'
        if trusted: current_delivery_state().analysis=evidence
        yield 'data: '+json.dumps({'object':'response','status':'completed','analysis_delivery':evidence})+'\n\n'
    for trusted in (False,True):
        stream=project_sse_stream(source(trusted),'task_001')
        outputs=[]
        async for item in stream:
            assert current_delivery_state() is None
            outputs.append(json.loads(item.removeprefix('data: ')))
        assert ('analysis_delivery' in outputs[-1]) is trusted
        assert current_delivery_state() is None

@pytest.mark.asyncio
async def test_converted_read_gaps_retain_the_authorized_original_attachment_identity():
    from bank_runtime.delivery_state import begin_delivery_state, end_delivery_state
    middleware=BankRuntimeGatewayMiddleware(Client())
    middleware.converted_sources['converted-f2']='f2'
    await invoke(middleware, 'MinerU__parse_documents', {'documents':[{'file_id':'f1'},{'file_id':'converted-f2'}]},
        {'items':[{'file_id':'f1','status':'completed','content_mode':'inline','document_ref':'good','markdown':'流程正文'},
                  {'file_id':'converted-f2','status':'failed','error_code':'DOCUMENT_PARSE_FAILED'}]})
    state,token=begin_delivery_state('task_001')
    try:
        async def model(**kwargs):
            return ChatResponse(id='partial',content=[TextBlock(text='第一份材料的审批流程。')],is_last=True)
        await middleware.on_model_call(None,{},model)
        assert state.analysis['gaps']==[{'kind':'document','file_id':'f2','impact':'scope_unread'}]
    finally:
        end_delivery_state(token)


@pytest.mark.asyncio
async def test_repeated_structured_query_prompts_reuse_before_stall_failure():
    from bank_runtime.gateway.document_reads import DocumentReadLedger
    from test_document_reads_v2 import observe_parse, aggregate_evidence
    middleware = BankRuntimeGatewayMiddleware(Client())
    observe_parse(middleware.document_reads)
    for _ in range(5):
        aggregate_evidence(middleware.document_reads)
    assert not middleware.document_reads.pending
    async def model(**kwargs):
        messages = str(kwargs['messages'])
        assert '直接复用' in messages and '无需为统计再逐行读完文件' in messages
        return ChatResponse(id='answer', content=[TextBlock(text='支行01金额合计为3')], is_last=True)
    await middleware.on_model_call(None, {}, model)

@pytest.mark.asyncio
async def test_document_call_uses_remaining_budget_and_expired_close_releases_stream():
    import time
    from bank_runtime.model_reliability import BankModelReliability
    from qwenpaw.drivers.mcp_context import current_mcp_timeout
    policy = BankModelReliability(1200)
    middleware = BankRuntimeGatewayMiddleware(Client(), model_reliability=policy)
    closed = []
    async def handler():
        assert 1190 < current_mcp_timeout().total_seconds() <= 1200
        try: yield 'first'
        finally: closed.append(True)
    stream = middleware._authorized_native_call('MinerU__parse_documents',
        {'documents':[{'file_id':'f1','file_ref':'r1'}]}, handler)
    assert await anext(stream) == 'first'
    assert current_mcp_timeout() is None
    policy.deadline = time.monotonic() - 1
    await stream.aclose()
    assert closed == [True]
    assert current_mcp_timeout() is None
