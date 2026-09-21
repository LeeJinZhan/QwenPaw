import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from agentscope.message import ToolCallBlock
from agentscope.model import ChatResponse

from bank_runtime.artifact_tools import ArtifactInputRetryExhaustedError
from bank_runtime.gateway.middleware import BankRuntimeGatewayMiddleware


@pytest.mark.asyncio
@pytest.mark.parametrize('streamed', [False, True])
async def test_malformed_model_arguments_share_budget_before_gateway(streamed):
    middleware = BankRuntimeGatewayMiddleware(None)
    seen = []
    async def model(**kwargs):
        seen.append(kwargs)
        response = ChatResponse(content=[ToolCallBlock(
            id=f'call-{len(seen)}', name='artifact_generate',
            input='{"artifact_type":"docx","output_name":"report.docx"',
        )], is_last=True)
        if not streamed:
            return response
        async def chunks():
            yield ChatResponse(content=[ToolCallBlock(id='partial', name='artifact_generate', input='{')], is_last=False)
            yield response
        return chunks()
    for _ in range(2):
        response = await middleware.on_model_call(None, {"tools": [{"function": {"name": "artifact_generate"}}]}, model)
        if streamed:
            _ = [chunk async for chunk in response]
    assert middleware.artifact_input_failures == 2
    assert 'content' in str(seen[1].get('messages'))
    with pytest.raises(ArtifactInputRetryExhaustedError):
        await middleware.on_model_call(None, {"tools": [{"function": {"name": "artifact_generate"}}]}, model)
    assert len(seen) == 2


@pytest.mark.asyncio
async def test_valid_final_arguments_do_not_count_partial_stream_as_failure():
    middleware = BankRuntimeGatewayMiddleware(None)
    payload = {'artifact_type': 'docx', 'title': '汇报', 'content': {'paragraphs': ['新增“同步”选项及 "OA" 图表。']}}
    async def model(**kwargs):
        async def chunks():
            yield ChatResponse(content=[ToolCallBlock(id='one', name='artifact_generate', input='{')], is_last=False)
            yield ChatResponse(content=[ToolCallBlock(id='one', name='artifact_generate', input=json.dumps(payload))], is_last=True)
        return chunks()
    response = await middleware.on_model_call(None, {"tools": [{"function": {"name": "artifact_generate"}}]}, model)
    chunks = [chunk async for chunk in response]
    assert json.loads(chunks[-1].content[0].input) == payload
    assert middleware.artifact_input_failures == 0


@pytest.mark.asyncio
async def test_malformed_and_runtime_validation_share_one_correction():
    from types import SimpleNamespace
    from agentscope.permission import PermissionBehavior
    from bank_runtime.gateway.client import GatewayError
    from bank_runtime.gateway.middleware import GatewayPermissionEngine
    class Client:
        async def preflight(self, *args, **kwargs):
            raise GatewayError('invalid', code='ARTIFACT_VALIDATION_FAILED')
    middleware = BankRuntimeGatewayMiddleware(Client())
    async def model(**kwargs):
        return ChatResponse(content=[ToolCallBlock(id='bad', name='artifact_generate', input='{')], is_last=True)
    await middleware.on_model_call(None, {"tools": [{"function": {"name": "artifact_generate"}}]}, model)
    engine = GatewayPermissionEngine(None, middleware)
    decision = await engine.check_permission(SimpleNamespace(name='artifact_generate'), {'artifact_type': 'docx'})
    assert decision.behavior == PermissionBehavior.DENY
    assert middleware.artifact_input_failures == 2
    with pytest.raises(ArtifactInputRetryExhaustedError):
        await middleware.on_model_call(None, {"tools": [{"function": {"name": "artifact_generate"}}]}, model)
    with pytest.raises(ArtifactInputRetryExhaustedError):
        middleware._check_file_completion()
