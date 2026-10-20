from bank_runtime.gateway.middleware import BankRuntimeGatewayMiddleware
from bank_runtime.gateway.completion import operation_keys


def test_delivery_conversion_does_not_require_read_without_frozen_intent():
    worker = BankRuntimeGatewayMiddleware(None)
    assert not worker._requires_conversion_read({"source_type": "session_file", "target_format": "docx", "purpose": "delivery"})
    assert worker._requires_conversion_read({"source_type": "session_file", "target_format": "docx", "purpose": "read"})


def test_distinct_content_same_name_cannot_clear_failure():
    a = {"artifact_type": "docx", "title": "报告", "content": {"paragraphs": ["第一份"]}}
    b = {**a, "content": {"paragraphs": ["第二份"]}}
    assert operation_keys("artifact_generate", a) != operation_keys("artifact_generate", b)
    assert operation_keys("artifact_generate", a) != operation_keys("artifact_revise", a)


import pytest
from agentscope.message import TextBlock
from agentscope.model import ChatResponse

@pytest.mark.asyncio
async def test_missing_material_reply_is_preserved_without_forced_tool_retry():
    worker = BankRuntimeGatewayMiddleware(None)
    calls = []
    async def model(**kwargs):
        calls.append(kwargs)
        return ChatResponse(content=[TextBlock(text="请提供格式说明和待修改报告。")], is_last=True)
    async def reply(**kwargs):
        yield await worker.on_model_call(None, {"tools": [{"function": {"name": "artifact_generate"}}]}, model)
    result = [item async for item in worker.on_reply(None, {}, reply)]
    assert result[0].content[0].text == "请提供格式说明和待修改报告。"
    assert len(calls) == 1
    assert calls[0].get("tool_choice") is None
    assert calls[0]["tools"][0]["function"]["name"] == "artifact_generate"

@pytest.mark.asyncio
async def test_failed_operation_does_not_stream_a_success_claim():
    from bank_runtime.artifact_tools import FileOperationsIncompleteError
    worker = BankRuntimeGatewayMiddleware(None)
    worker.unresolved_file_operations.add("artifact:failed-call")
    async def model(**kwargs):
        async def chunks():
            yield ChatResponse(content=[TextBlock(text="已生成")], is_last=False)
            yield ChatResponse(content=[TextBlock(text="已生成文件")], is_last=True)
        return chunks()
    with pytest.raises(FileOperationsIncompleteError):
        await worker.on_model_call(None, {}, model)
