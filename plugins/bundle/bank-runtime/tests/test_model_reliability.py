import asyncio
from pathlib import Path
import sys
import pytest
from agentscope.message import TextBlock, ThinkingBlock, ToolCallBlock, UserMsg
from agentscope.model import ChatResponse
from qwenpaw.exceptions import ModelExecutionException
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bank_runtime.model_reliability import BankModelReliability

async def collect(policy, model, **kwargs):
    return [c async for c in await policy.call(model, **kwargs)]

@pytest.mark.asyncio
async def test_no_output_retries_once_and_latches_failure():
    calls = []
    async def model(**kwargs):
        calls.append(kwargs)
        raise TimeoutError('private upstream details')
    policy = BankModelReliability(10, idle_seconds=.02)
    with pytest.raises(Exception) as caught:
        await collect(policy, model)
    assert caught.value.error_code == 'MODEL_TIMEOUT'
    assert len(calls) == 2
    with pytest.raises(Exception):
        await collect(policy, model)
    assert len(calls) == 2
    assert 'private' not in str(caught.value)

@pytest.mark.asyncio
async def test_partial_output_timeout_never_restarts_request():
    calls = []
    async def model(**kwargs):
        calls.append(kwargs)
        async def stream():
            yield ChatResponse([TextBlock(text='已输出')], False)
            raise TimeoutError()
        return stream()
    with pytest.raises(Exception) as caught:
        await collect(BankModelReliability(10), model)
    assert caught.value.error_code == 'MODEL_TIMEOUT'
    assert len(calls) == 1

@pytest.mark.asyncio
async def test_empty_heartbeats_do_not_reset_idle_and_stream_is_closed():
    closed = []
    async def model(**kwargs):
        async def stream():
            try:
                while True:
                    await asyncio.sleep(.001)
                    yield ChatResponse([], False)
            finally:
                closed.append(True)
        return stream()
    with pytest.raises(Exception) as caught:
        await collect(BankModelReliability(1, idle_seconds=.01), model)
    assert caught.value.error_code == 'MODEL_TIMEOUT'
    assert len(closed) == 2

@pytest.mark.asyncio
async def test_truncated_tool_is_discarded_and_complete_proposal_checked():
    calls = []
    async def model(**kwargs):
        calls.append(kwargs)
        async def stream():
            if len(calls) == 1:
                yield ChatResponse([ToolCallBlock(id='old', name='artifact_generate', input='{"content":')], False)
                raise ModelExecutionException('upstream', details={'finish_reason': 'length'})
            yield ChatResponse([ToolCallBlock(id='new', name='artifact_generate', input='{"content":{"paragraphs":["完整正文"]}}')], True)
        return stream()
    chunks = await collect(BankModelReliability(10), model, messages=[UserMsg('user', '生成文件')],
        tools=[{'type':'function','function':{'name':'artifact_generate'}}])
    assert len(calls) == 2
    assert all(b.id != 'old' for c in chunks for b in c.content if isinstance(b, ToolCallBlock))
    assert chunks[-1].content[0].id == 'new'
    assert len(calls[0]['messages']) == 1

@pytest.mark.asyncio
async def test_text_continuation_is_buffered_and_uses_original_response_id():
    calls = []
    async def model(**kwargs):
        calls.append(kwargs)
        async def stream():
            if len(calls) == 1:
                yield ChatResponse([TextBlock(text='第一部分。')], False, id='original')
                raise ModelExecutionException('upstream', details={'finish_reason':'length'})
            yield ChatResponse([TextBlock(text='第二部分。')], False, id='retry')
            yield ChatResponse([TextBlock(text='第二部分。')], True, id='retry')
        return stream()
    chunks = await collect(BankModelReliability(10), model, messages=[UserMsg('user','说明')])
    assert len(chunks) == 2
    assert chunks[-1].id == 'original'
    assert chunks[-1].content[0].text == '第一部分。第二部分。'
    assert calls[1]['tools'] == []
    assert len(calls[0]['messages']) == 1

@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['length', 'content_filter', 'invalid_tool', 'duplicate_text'])
async def test_failed_recovery_or_filter_does_not_get_more_attempts(mode):
    calls = []
    async def model(**kwargs):
        calls.append(kwargs)
        async def stream():
            if len(calls) == 1:
                yield ChatResponse([TextBlock(text='原始内容足够长，不能重复原始内容。')], False)
                raise ModelExecutionException('upstream', details={'finish_reason':'content_filter' if mode=='content_filter' else 'length'})
            if mode == 'length':
                raise ModelExecutionException('upstream', details={'finish_reason':'length'})
            block = ToolCallBlock(id='x',name='artifact_generate',input='{}') if mode=='invalid_tool' else TextBlock(text='原始内容足够长，不能重复原始内容。')
            yield ChatResponse([block], True)
        return stream()
    with pytest.raises(Exception):
        await collect(BankModelReliability(10), model, messages=[])
    assert len(calls) == (1 if mode=='content_filter' else 2)

@pytest.mark.asyncio
async def test_total_deadline_and_cancellation_never_retry():
    calls = []
    async def model(**kwargs):
        calls.append(1)
        await asyncio.sleep(1)
    with pytest.raises(Exception) as caught:
        await collect(BankModelReliability(.01, idle_seconds=10), model)
    assert caught.value.error_code == 'WORKER_TIMEOUT'
    assert len(calls) == 1
    async def cancelled(**kwargs):
        raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await collect(BankModelReliability(10), cancelled)

@pytest.mark.asyncio
async def test_thinking_is_progress_and_normal_text_remains_streamed():
    async def model(**kwargs):
        async def stream():
            yield ChatResponse([ThinkingBlock(thinking='思考中')], False)
            yield ChatResponse([TextBlock(text='结果')], False, id='r')
            yield ChatResponse([TextBlock(text='结果')], True, id='r')
        return stream()
    chunks = await collect(BankModelReliability(10), model)
    assert len(chunks) == 3
    assert chunks[1].content[0].text == '结果'

@pytest.mark.asyncio
async def test_error_hook_and_sse_keep_typed_cause_without_private_text():
    from types import SimpleNamespace
    from bank_runtime.artifact_tools import ArtifactDeliveryErrorHook
    from bank_runtime.events import CompactEventProjector
    error = ModelExecutionException('secret-model', details={'finish_reason':'length'})
    ctx = SimpleNamespace(error=error, extras={})
    await ArtifactDeliveryErrorHook().run(ctx)
    events = CompactEventProjector('task').project({'error':{'code':ctx.extras['_error_code'], 'message':ctx.extras['_error_text']}})
    assert events[0]['error_code'] == 'MODEL_OUTPUT_TRUNCATED'
    assert 'secret' not in str(events)

@pytest.mark.asyncio
async def test_real_retry_wrapper_does_not_multiply_bank_attempts():
    from types import SimpleNamespace
    from qwenpaw.providers.retry_chat_model import RetryChatModel, RetryConfig
    from qwenpaw.providers.retry_scope import external_retry_owner
    import httpx
    calls=[]
    class Model:
        model='bank-retry-test'
        stream=True
        context_size=32768
        parameters=None
        async def __call__(self, **kwargs):
            calls.append(1)
            raise httpx.ReadTimeout('private')
    wrapped=RetryChatModel(Model(), retry_config=RetryConfig(max_retries=3))
    with pytest.raises(Exception) as caught:
        await collect(BankModelReliability(10), wrapped)
    assert caught.value.error_code=='MODEL_TIMEOUT'
    assert len(calls)==2
    assert external_retry_owner.get() is False

@pytest.mark.asyncio
@pytest.mark.parametrize('model_id', ['deepseek-v4-flash', 'qwen3-27b'])
async def test_actual_provider_nonstream_length_is_recovered_once_with_first_system(monkeypatch, model_id):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from agentscope.credential._openai import OpenAICredential
    from openai.types.chat import ChatCompletion
    from qwenpaw.providers.openai_chat_model_compat import OpenAIChatModelCompat
    seen=[]
    async def api(*, messages, **kwargs):
        assert messages[0]['role']=='system'
        assert all(m['role']!='system' for m in messages[1:])
        seen.append(messages)
        return ChatCompletion(id='r',created=0,model=model_id,object='chat.completion',choices=[{
            'index':0,'finish_reason':'length' if len(seen)==1 else 'stop',
            'message':{'role':'assistant','content':'完整结果'}}])
    monkeypatch.setattr('openai.AsyncClient', lambda **kwargs: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(side_effect=api)))))
    model=OpenAIChatModelCompat(credential=OpenAICredential(id='probe',api_key='unused',base_url='http://127.0.0.1:1/v1'),model=model_id,stream=False)
    from agentscope.message import SystemMsg
    history=[SystemMsg('system','base'),UserMsg('user','说明'),SystemMsg('system','trusted guidance')]
    async def call(**kwargs):
        return await model._call_api(model_id,kwargs['messages'],kwargs.get('tools'))
    chunks=await collect(BankModelReliability(10),call,messages=history)
    assert len(seen)==2
    assert chunks[-1].content[0].text=='完整结果'
    assert len(history)==3

@pytest.mark.asyncio
async def test_recovery_cannot_drop_second_call_to_same_tool():
    calls=[]
    async def model(**kwargs):
        calls.append(1)
        async def stream():
            if len(calls)==1:
                yield ChatResponse([ToolCallBlock(id='a',name='artifact_generate',input='{'),ToolCallBlock(id='b',name='artifact_generate',input='{')],False)
                raise ModelExecutionException('upstream',details={'finish_reason':'length'})
            yield ChatResponse([ToolCallBlock(id='c',name='artifact_generate',input='{}')],True)
        return stream()
    with pytest.raises(Exception):
        await collect(BankModelReliability(10),model,tools=[{'function':{'name':'artifact_generate'}}])

@pytest.mark.asyncio
@pytest.mark.parametrize('model_id', ['deepseek-v4-flash', 'qwen3-27b'])
async def test_real_sdk_stream_continuation_preserves_prefix_and_retry_scope(monkeypatch, model_id):
    from types import SimpleNamespace
    from agentscope.credential._openai import OpenAICredential
    from agentscope.message import SystemMsg
    from qwenpaw.providers.openai_chat_model_compat import OpenAIChatModelCompat
    requests, clients, closed = [], [], []
    class WireStream:
        def __init__(self, items): self.items = iter(items)
        async def __aenter__(self): return self
        async def __aexit__(self, *args): closed.append(True)
        def __aiter__(self): return self
        async def __anext__(self):
            try: return next(self.items)
            except StopIteration: raise StopAsyncIteration
    def chunk(content=None, reason=None, thinking=None):
        return SimpleNamespace(usage=None, choices=[SimpleNamespace(finish_reason=reason,
            delta=SimpleNamespace(content=content, reasoning_content=thinking, tool_calls=None))])
    async def api(**kwargs):
        requests.append(kwargs)
        assert kwargs['messages'][0]['role'] == 'system'
        assert all(m['role'] != 'system' for m in kwargs['messages'][1:])
        return WireStream([chunk(thinking='思考'), chunk(content='前文。' if len(requests)==1 else '后文。'),
                           chunk(reason='length' if len(requests)==1 else 'stop')])
    def client(**kwargs):
        clients.append(kwargs)
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=api)))
    monkeypatch.setattr('openai.AsyncClient', client)
    model = OpenAIChatModelCompat(credential=OpenAICredential(id='probe',api_key='unused',base_url='http://127.0.0.1:1/v1'),
        model=model_id, stream=True, max_retries=0)
    original_kwargs = dict(model.client_kwargs)
    history = [SystemMsg('system','base'), UserMsg('user','回答')]
    result = await collect(BankModelReliability(10), model, messages=history)
    assert len(requests) == 2
    assert result[-1].content[0].text == '前文。后文。'
    assert len(closed) == 2
    assert all(c['max_retries'] == 0 for c in clients)
    assert model.client_kwargs == original_kwargs
    assert len(history) == 2

@pytest.mark.asyncio
@pytest.mark.parametrize('target_format', ['docx', 'pptx'])
async def test_existing_parameter_recovery_shares_budget_and_keeps_actual_failure(target_format):
    from types import SimpleNamespace
    from bank_runtime.artifact_tools import ArtifactDeliveryIntent
    from bank_runtime.gateway.middleware import BankRuntimeGatewayMiddleware
    policy = BankModelReliability(10)
    middleware = BankRuntimeGatewayMiddleware(None, model_reliability=policy,
        artifact_intent=ArtifactDeliveryIntent('generate', target_format))
    middleware._artifact_turn_state = SimpleNamespace(invoked=False, failed=False, replan_count=0)
    calls = []
    async def model(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise ModelExecutionException('private', details={'finish_reason':'error'})
        raise TimeoutError('private')
    with pytest.raises(Exception) as caught:
        await middleware.on_model_call(None, {'tools':[{'function':{'name':'artifact_generate'}}]}, model)
    assert caught.value.error_code == 'MODEL_TIMEOUT'
    assert len(calls) == 2
    assert not middleware._prepared

@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['no_output', 'truncation'])
async def test_published_zero_budget_disables_only_selected_recovery(kind):
    calls = []
    async def model(**kwargs):
        calls.append(kwargs)
        if kind == 'no_output': raise TimeoutError()
        raise ModelExecutionException('private', details={'finish_reason':'length'})
    policy = BankModelReliability(10, **({'no_output_retry_attempts':0} if kind=='no_output' else {'truncation_recovery_attempts':0}))
    with pytest.raises(Exception) as error:
        await collect(policy, model)
    assert error.value.error_code == ('MODEL_TIMEOUT' if kind=='no_output' else 'MODEL_OUTPUT_TRUNCATED')
    assert len(calls) == 1
