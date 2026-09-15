import json
import sys
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from agentscope.message import TextBlock
from agentscope.model import ChatResponse
from qwenpaw.exceptions import ModelExecutionException
from bank_runtime.artifact_tools import ArtifactDeliveryIntent
from bank_runtime.docx_draft import controlled_docx_call
from bank_runtime.gateway.middleware import BankRuntimeGatewayMiddleware
from bank_runtime.artifact_tools import ArtifactToolNotInvokedError

HEADER = '{"title":"研究文章","document_type":"article","layout_kind":"standard_document"}'
BODY = '保留正文中的 "引号"、换行与\\字符。\n\n第二段完整内容。'

def draft(text=HEADER+'\n\n'+BODY, final=True):
    return ChatResponse(content=[TextBlock(text=text)], is_last=final)


def test_draft_encodes_body_once_and_preserves_every_character():
    response=controlled_docx_call(draft('\n\n'+HEADER+'\n\n'+BODY))
    payload=json.loads(response.content[0].input)
    assert payload['content'] == BODY
    assert payload['delivery_plan']['layout_kind'] == 'standard_document'


@pytest.mark.parametrize('value', [draft(final=False), draft(HEADER), draft(HEADER+'\n '),
    draft(HEADER.replace('standard_document','official_document')+'\n正文'),
    draft('{"title":"研究文章","document_type":"article","layout_kind":"standard_document","path":"/tmp/x"}\n正文')],
    ids=['unfinished', 'header_only', 'blank_body', 'official', 'unknown_field'])
def test_incomplete_official_or_unknown_draft_is_not_a_tool_call(value):
    with pytest.raises(ValueError): controlled_docx_call(value)


@pytest.mark.asyncio
async def test_provider_failure_recovers_once_without_executing_a_file_operation():
    middleware=BankRuntimeGatewayMiddleware(None, artifact_intent=ArtifactDeliveryIntent('generate','docx'))
    middleware._artifact_turn_state=SimpleNamespace(invoked=False, failed=False, replan_count=0)
    calls=[]
    async def model(**kwargs):
        calls.append(kwargs)
        if len(calls)==1: raise ModelExecutionException('unit', details={'finish_reason':'error'})
        return draft()
    result=await middleware.on_model_call(None, {'tools':[{'function':{'name':'artifact_generate'}}]}, model)
    if not isinstance(result, ChatResponse): result=[c async for c in result][-1]
    assert result.content[0].name == 'artifact_generate'
    assert calls[1]['tools'] == []
    assert calls[1]['tool_choice'].mode == 'none'
    assert middleware._artifact_turn_state.invoked
    assert not middleware._prepared  # no preflight permit, no execution


@pytest.mark.asyncio
@pytest.mark.parametrize('reason,layout,sources,visible', [
    ('length','',(),True), ('content_filter','',(),True),
    ('error','official_document',(),True), ('error','',('file_source',),True),
    ('error','',(),False),
])
async def test_recovery_does_not_expand_authority_or_downgrade(reason,layout,sources,visible):
    middleware=BankRuntimeGatewayMiddleware(None, artifact_intent=ArtifactDeliveryIntent('generate','docx',source_refs=sources,layout_kind=layout))
    middleware._artifact_turn_state=SimpleNamespace(invoked=False, failed=False, replan_count=0)
    calls=[]
    async def model(**kwargs):
        calls.append(kwargs); raise ModelExecutionException('unit', details={'finish_reason':reason})
    with pytest.raises(ModelExecutionException):
        await middleware.on_model_call(None, {'tools':[{'function':{'name':'artifact_generate'}}] if visible else []}, model)
    assert len(calls)==1


@pytest.mark.asyncio
async def test_failure_during_required_tool_replan_can_recover():
    middleware = BankRuntimeGatewayMiddleware(None, artifact_intent=ArtifactDeliveryIntent('generate', 'docx'))
    middleware._artifact_turn_state = SimpleNamespace(invoked=False, failed=False, replan_count=0)
    calls = []
    async def model(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return draft('我将生成文档。')
        if len(calls) == 2:
            raise ModelExecutionException('unit', details={'finish_reason': 'error'})
        return draft()
    result = await middleware.on_model_call(None, {'tools': [{'function': {'name': 'artifact_generate'}}]}, model)
    assert result.content[0].name == 'artifact_generate'
    assert len(calls) == 3
    assert calls[-1]['tools'] == []


@pytest.mark.asyncio
async def test_failed_draft_is_not_repeated_by_model_retry():
    middleware = BankRuntimeGatewayMiddleware(None, artifact_intent=ArtifactDeliveryIntent('generate', 'docx'))
    middleware._artifact_turn_state = SimpleNamespace(invoked=False, failed=False, replan_count=0)
    calls = []
    async def model(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise ModelExecutionException('unit', details={'finish_reason': 'error'})
        return draft('不完整的文档参数')
    kwargs = {'tools': [{'function': {'name': 'artifact_generate'}}]}
    with pytest.raises(ModelExecutionException):
        await middleware.on_model_call(None, kwargs, model)
    with pytest.raises(ArtifactToolNotInvokedError):
        await middleware.on_model_call(None, kwargs, model)
    assert len(calls) == 2
    assert not middleware._artifact_turn_state.invoked
    assert not middleware._prepared


@pytest.mark.asyncio
async def test_stream_error_discards_partial_text_and_replays_only_tool_call():
    middleware = BankRuntimeGatewayMiddleware(None, artifact_intent=ArtifactDeliveryIntent('generate', 'docx'))
    middleware._artifact_turn_state = SimpleNamespace(invoked=False, failed=False, replan_count=0)
    calls = []
    async def model(**kwargs):
        calls.append(kwargs)
        first = len(calls) == 1
        async def stream():
            yield draft('不得展示的残缺内容', final=False)
            if first:
                raise ModelExecutionException('unit', details={'finish_reason': 'error'})
            yield draft()
        return stream()
    result = await middleware.on_model_call(None, {'tools': [{'function': {'name': 'artifact_generate'}}]}, model)
    chunks = [chunk async for chunk in result]
    assert len(chunks) == 1
    assert chunks[0].is_last
    assert all(block.type == 'tool_call' for block in chunks[0].content)
    assert json.loads(chunks[0].content[0].input)['content'] == BODY


@pytest.mark.asyncio
async def test_recovered_parameters_still_require_runtime_preflight():
    from agentscope.permission import PermissionBehavior
    from bank_runtime.gateway.client import GatewayError
    from bank_runtime.gateway.middleware import GatewayPermissionEngine

    class DeniedClient:
        async def preflight(self, *args, **kwargs):
            raise GatewayError('denied', code='POLICY_BLOCKED')

    class UnusedGuard:
        context = None
        async def check_permission(self, *args, **kwargs):
            pytest.fail('failed preflight must stop before guard')

    middleware = BankRuntimeGatewayMiddleware(DeniedClient(), artifact_intent=ArtifactDeliveryIntent('generate', 'docx'))
    proposal = controlled_docx_call(draft())
    engine = GatewayPermissionEngine(UnusedGuard(), middleware)
    decision = await engine.check_permission(SimpleNamespace(name='artifact_generate'), json.loads(proposal.content[0].input))
    assert decision.behavior == PermissionBehavior.DENY
    assert not middleware._prepared
