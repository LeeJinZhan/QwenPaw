import json

import pytest
from agentscope.message import TextBlock, ToolCallBlock, ToolResultState
from agentscope.model import ChatResponse
from agentscope.tool import ToolResponse
from bank_runtime.gateway.middleware import BankRuntimeGatewayMiddleware
from bank_runtime.artifact_tools import FileOperationsIncompleteError


class Client:
    def __init__(self):
        self.reports = []
        self.executions = []

    async def report_guard(self, *args): pass

    async def report_result(self, *args): self.reports.append(args)

    async def execute_runtime_tool(self, *args):
        self.executions.append(args)
        return {"status": "success", "result": {"artifact_status": "succeeded", "generated_file_ids": ["report"]}}


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
