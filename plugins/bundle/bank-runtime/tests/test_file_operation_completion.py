import json
from types import SimpleNamespace

import pytest
from agentscope.message import TextBlock
from agentscope.tool import ToolResponse
from bank_runtime.gateway.completion import operation_keys, parse_outcomes
from bank_runtime.gateway.middleware import BankRuntimeGatewayMiddleware
from bank_runtime.artifact_tools import FileOperationsIncompleteError


def test_parse_tracks_individual_files_in_partial_response():
    content = [TextBlock(type="text", text=json.dumps({"status": "partial", "items": [
        {"file_id": "f1", "status": "completed"}, {"file_id": "f2", "status": "failed"}]}))]
    assert list(parse_outcomes(content)) == [("parse:f1", True), ("parse:f2", False)]
    assert operation_keys("MinerU__parse_documents", {"documents": [{"file_id": "f1"}, {"file_id": "f2"}]}) == {"parse:f1", "parse:f2"}


def test_different_source_success_cannot_clear_another_failure():
    keys = operation_keys("artifact_generate", {"artifact_type": "xlsx", "source_refs": [{"source_id": "f1"}]})
    keys.difference_update(operation_keys("artifact_generate", {"artifact_type": "xlsx", "source_refs": [{"source_id": "f2"}]}))
    assert keys
    keys.difference_update(operation_keys("artifact_generate", {"artifact_type": "xlsx", "source_refs": [{"source_id": "f1"}]}))
    assert not keys


@pytest.mark.asyncio
async def test_turn_cannot_complete_with_unresolved_file_processing():
    middleware = BankRuntimeGatewayMiddleware(None)
    async def next_handler(**kwargs):
        middleware.unresolved_file_operations.add("parse:f1")
        yield "candidate"
    with pytest.raises(FileOperationsIncompleteError):
        _ = [item async for item in middleware.on_reply(None, {}, next_handler)]


@pytest.mark.asyncio
async def test_recovered_operation_can_complete():
    middleware = BankRuntimeGatewayMiddleware(None)
    async def next_handler(**kwargs):
        middleware.unresolved_file_operations.add("parse:f1")
        middleware.unresolved_file_operations.discard("parse:f1")
        yield "answer"
    assert [item async for item in middleware.on_reply(None, {}, next_handler)] == ["answer"]

@pytest.mark.asyncio
async def test_partial_parser_result_keeps_only_failed_file_unresolved():
    from agentscope.message import ToolCallBlock, ToolResultState
    class Client:
        async def report_guard(self, *args): pass
        async def report_result(self, *args): pass
    middleware = BankRuntimeGatewayMiddleware(Client())
    payload = {"documents": [{"file_id":"f1"}, {"file_id":"f2"}]}
    middleware.prepare("MinerU__parse_documents", payload, {"tool_call_id":"call"})
    async def parser():
        yield ToolResponse(id="call", state=ToolResultState.SUCCESS, content=[TextBlock(type="text", text=json.dumps({"status":"partial","items":[{"file_id":"f1","status":"completed"},{"file_id":"f2","status":"failed"}]}))])
    call = ToolCallBlock(id="call", name="MinerU__parse_documents", input=json.dumps(payload))
    _ = [item async for item in middleware.on_acting(None, {"tool_call":call}, parser)]
    assert middleware.unresolved_file_operations == {"parse:f2"}

@pytest.mark.asyncio
async def test_parser_without_final_response_remains_incomplete():
    from agentscope.message import ToolCallBlock
    class Client:
        async def report_guard(self, *args): pass
        async def report_result(self, *args): pass
    middleware = BankRuntimeGatewayMiddleware(Client())
    payload = {"documents": [{"file_id": "f1"}]}
    middleware.prepare("MinerU__parse_documents", payload, {"tool_call_id": "call"})
    async def parser():
        if False:
            yield None
    call = ToolCallBlock(id="call", name="MinerU__parse_documents", input=json.dumps(payload))
    _ = [item async for item in middleware.on_acting(None, {"tool_call": call}, parser)]
    assert middleware.unresolved_file_operations == {"parse:f1"}


@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.asyncio
async def test_mcp_structured_result_survives_tool_response_accumulation(failed):
    """Exercise the same MCP adapter/AgentScope merge as the deployed parser."""
    from agentscope.message import ToolCallBlock, ToolResultState
    from agentscope.tool import ToolChunk
    from mcp.types import CallToolResult, TextContent
    from qwenpaw.drivers.adapters.agentscope_tool import _blocks_from_value

    class Client:
        async def report_guard(self, *args): pass
        async def report_result(self, *args): pass

    result = {"status": "partial" if failed else "completed", "items": [
        {"file_id": "f1", "status": "completed"},
        {"file_id": "f2", "status": "failed" if failed else "completed"},
    ]}
    mcp_result = CallToolResult(
        content=[TextContent(type="text", text=json.dumps(result))],
        structuredContent=result, isError=False,
    )
    response = ToolResponse(id="call").append_chunk(ToolChunk(
        content=_blocks_from_value(mcp_result), state=ToolResultState.SUCCESS,
    ))
    middleware = BankRuntimeGatewayMiddleware(Client())
    payload = {"documents": [{"file_id": "f1"}, {"file_id": "f2"}]}

    async def parser():
        yield response

    async def reply(**kwargs):
        middleware.prepare("MinerU__parse_documents", payload, {"tool_call_id": "call"})
        call = ToolCallBlock(id="call", name="MinerU__parse_documents", input=json.dumps(payload))
        async for item in middleware.on_acting(None, {"tool_call": call}, parser):
            yield item

    if failed:
        with pytest.raises(FileOperationsIncompleteError):
            _ = [item async for item in middleware.on_reply(None, {}, reply)]
        assert middleware.unresolved_file_operations == {"parse:f2"}
    else:
        assert [item async for item in middleware.on_reply(None, {}, reply)] == [response]
        assert not middleware.unresolved_file_operations


@pytest.mark.parametrize("separator", ["", "\n", " \n\t"])
def test_concatenated_json_results_keep_conflicting_file_status_unresolved(separator):
    success = json.dumps({"items": [{"file_id": "f1", "status": "completed"}]})
    failure = json.dumps({"items": [{"file_id": "f1", "status": "failed"}]})
    for values in [(success, failure), (failure, success)]:
        content = [TextBlock(type="text", text=separator.join(values))]
        assert list(parse_outcomes(content)) == [("parse:f1", False)]


@pytest.mark.parametrize("suffix", ["truncated", "{", "\nDone"])
def test_invalid_json_suffix_cannot_prove_parse_completion(suffix):
    success = json.dumps({"items": [{"file_id": "f1", "status": "completed"}]})
    assert list(parse_outcomes([TextBlock(type="text", text=success + suffix)])) == []


def test_conflicting_status_across_blocks_keeps_failure():
    content = [TextBlock(type="text", text=json.dumps({"items": [
        {"file_id": "f1", "status": status}]})) for status in ("failed", "completed")]
    assert list(parse_outcomes(content)) == [("parse:f1", False)]
