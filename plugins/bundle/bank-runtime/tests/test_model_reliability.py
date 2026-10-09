import asyncio
from pathlib import Path
import sys
import pytest
from agentscope.message import TextBlock, ThinkingBlock, ToolCallBlock, UserMsg
from agentscope.model import ChatResponse
from qwenpaw.exceptions import ModelExecutionException
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bank_runtime.model_reliability import BankModelReliability

@pytest.mark.parametrize("value", [10**400, True, False])
def test_untrusted_task_budget_overflow_and_boolean_use_safe_default(value):
    from bank_runtime.model_reliability import positive_seconds
    assert positive_seconds(value, 3600, maximum=3600) == 3600

@pytest.mark.parametrize('status, expected', [
    (429, 'http_status_429'),
    (502, 'http_status_502'),
    ('503', 'http_status_503'),
    ('private provider context', 'unspecified'),
    ('503 private provider context', 'unspecified'),
    (True, 'unspecified'),
    (200, 'unspecified'),
])
def test_api_error_diagnostic_only_exposes_bounded_http_error_codes(status, expected):
    import httpx
    import openai
    from bank_runtime.model_reliability import failure_diagnostic
    error = openai.APIError('private model context',
        request=httpx.Request('POST', 'https://provider.invalid/chat'),
        body={'code': status, 'message': 'private provider context'})
    assert failure_diagnostic(error) == expected

def api_stream_error(code):
    import httpx
    import openai
    return openai.APIError('private model context',
        request=httpx.Request('POST', 'https://provider.invalid/chat'),
        body={'code': code, 'message': 'private provider context'})

@pytest.mark.parametrize('status, expected', [
    (429, 'MODEL_UPSTREAM_UNAVAILABLE'),
    (502, 'MODEL_UPSTREAM_UNAVAILABLE'),
    ('503', 'MODEL_UPSTREAM_UNAVAILABLE'),
    (400, 'MODEL_REQUEST_REJECTED'),
    ('401', 'MODEL_REQUEST_REJECTED'),
    (403, 'MODEL_REQUEST_REJECTED'),
    ('unknown', 'MODEL_EXECUTION_ERROR'),
    ('503 private provider context', 'MODEL_EXECUTION_ERROR'),
    (True, 'MODEL_EXECUTION_ERROR'),
    (200, 'MODEL_EXECUTION_ERROR'),
])
def test_streaming_sdk_status_uses_existing_http_failure_semantics(status, expected):
    from bank_runtime.model_reliability import failure_code
    assert failure_code(api_stream_error(status)) == expected

def test_actual_http_status_has_priority_over_stream_error_code():
    from bank_runtime.model_reliability import failure_code, failure_diagnostic
    error = api_stream_error(503)
    error.status_code = 400
    assert failure_code(error) == 'MODEL_REQUEST_REJECTED'
    assert failure_diagnostic(error) == 'http_status_400'

async def collect(policy, model, **kwargs):
    return [c async for c in await policy.call(model, **kwargs)]

@pytest.mark.asyncio
async def test_stream_error_retries_only_current_model_call_after_completed_tool():
    policy = BankModelReliability(10)
    proposal_calls, response_calls = [], []
    async def propose(**kwargs):
        proposal_calls.append(kwargs)
        return ChatResponse([ToolCallBlock(id='saved', name='chart_generate', input='{}')], True)
    proposal = await collect(policy, propose)
    assert proposal[-1].content[0].id == 'saved'
    history = [UserMsg('user', 'tool result already saved')]
    async def respond(**kwargs):
        response_calls.append(kwargs)
        if len(response_calls) == 1:
            raise api_stream_error(503)
        return ChatResponse([TextBlock(text='图表已生成。')], True)
    result = await collect(policy, respond, messages=history, tools=[])
    assert result[-1].content[0].text == '图表已生成。'
    assert len(proposal_calls) == 1
    assert len(response_calls) == 2
    assert all(call['messages'] is history and call['tools'] == [] for call in response_calls)
    assert policy.recovery_used is True
    async def later(**kwargs):
        response_calls.append(kwargs)
        raise api_stream_error(429)
    with pytest.raises(Exception) as caught:
        await collect(policy, later)
    assert caught.value.error_code == 'MODEL_UPSTREAM_UNAVAILABLE'
    assert len(response_calls) == 3

@pytest.mark.asyncio
@pytest.mark.parametrize('block', [TextBlock(text='partial'), ThinkingBlock(thinking='partial'),
    ToolCallBlock(id='partial', name='chart_generate', input='{')])
async def test_stream_error_after_any_output_never_retries(block):
    calls = []
    async def model(**kwargs):
        calls.append(kwargs)
        async def stream():
            yield ChatResponse([block], False)
            raise api_stream_error(503)
        return stream()
    with pytest.raises(Exception) as caught:
        await collect(BankModelReliability(10), model)
    assert caught.value.error_code == 'MODEL_UPSTREAM_UNAVAILABLE'
    assert len(calls) == 1

@pytest.mark.asyncio
@pytest.mark.parametrize('code, budget, expected', [
    (400, 1, 'MODEL_REQUEST_REJECTED'),
    (401, 1, 'MODEL_REQUEST_REJECTED'),
    ('unknown', 1, 'MODEL_EXECUTION_ERROR'),
    (503, 0, 'MODEL_UPSTREAM_UNAVAILABLE'),
])
async def test_stream_error_preserves_rejection_unknown_and_disabled_retry(code, budget, expected):
    calls = []
    async def model(**kwargs):
        calls.append(kwargs)
        raise api_stream_error(code)
    with pytest.raises(Exception) as caught:
        await collect(BankModelReliability(10, no_output_retry_attempts=budget), model)
    assert caught.value.error_code == expected
    assert len(calls) == 1

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
async def test_failure_log_exposes_safe_diagnostic_without_provider_content(caplog):
    async def model(**kwargs):
        raise ModelExecutionException('upstream', details={
            'stream_error': 'missing_finish_reason', 'provider_body': 'private model context',
        })
    with pytest.raises(Exception):
        await collect(BankModelReliability(10), model)
    assert 'reason=missing_finish_reason' in caplog.text
    assert 'error_type=ModelExecutionException' in caplog.text
    assert 'private model context' not in caplog.text

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
def test_task_budget_supports_one_hour_and_rejects_two_hour_extension():
    import time
    from bank_runtime.model_reliability import BankModelReliability
    before = time.monotonic()
    reliability = BankModelReliability(7200)
    assert 3599 <= reliability.deadline - before <= 3601


@pytest.mark.asyncio
@pytest.mark.parametrize('empty_blocks', [[], [TextBlock(text='  ')], [ThinkingBlock(thinking='private planning')]])
@pytest.mark.parametrize('next_block', [TextBlock(text='有效答复'), ToolCallBlock(id='next', name='read_range', input='{}')])
async def test_empty_completed_response_continues_only_current_call(empty_blocks, next_block):
    from agentscope.message import AssistantMsg, ToolResultBlock
    history = [UserMsg('user', '完成分析'), AssistantMsg('assistant', [
        ToolCallBlock(id='done', name='parse_documents', input='{}'),
        ToolResultBlock(id='done', name='parse_documents', output=[TextBlock(text='解析已完成')]),
    ])]
    schemas = [{'type': 'function', 'function': {'name': 'read_range'}}]
    calls = []
    policy = BankModelReliability(10)
    deadline = policy.deadline
    async def model(**kwargs):
        calls.append(kwargs)
        return ChatResponse(empty_blocks if len(calls) == 1 else [next_block], True)
    chunks = await collect(policy, model, messages=history, tools=schemas, tool_choice='auto')
    assert len(calls) == 2
    assert chunks[-1].content == [next_block]
    assert calls[1]['messages'][:-1] == history
    assert calls[1]['messages'][-1].role == 'system'
    assert calls[1]['tools'] is schemas
    assert calls[1]['tool_choice'] == 'auto'
    assert len(history) == 2
    assert policy.deadline == deadline
    assert policy.recovery_used is True


@pytest.mark.asyncio
async def test_repeated_thinking_only_latches_typed_failure_and_safe_diagnostic(caplog):
    calls = []
    async def model(**kwargs):
        calls.append(kwargs)
        return ChatResponse([ThinkingBlock(thinking='private planning')], True)
    policy = BankModelReliability(10)
    with pytest.raises(Exception) as caught:
        await collect(policy, model, messages=[], tools=[])
    assert caught.value.error_code == 'WORKER_EMPTY_RESPONSE'
    assert caught.value.details['attempts'] == 2
    assert len(calls) == 2
    assert 'reason=thinking_only' in caplog.text
    assert 'text_chars=0' in caplog.text and 'tool_calls=0' in caplog.text
    assert 'private planning' not in caplog.text
    with pytest.raises(Exception):
        await collect(policy, model)
    assert len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['disabled', 'used', 'expired'])
async def test_empty_response_respects_existing_recovery_allowance_and_deadline(mode):
    policy = BankModelReliability(10, no_output_retry_attempts=0 if mode == 'disabled' else 1)
    policy.recovery_used = mode == 'used'
    calls = []
    async def model(**kwargs):
        calls.append(kwargs)
        if mode == 'expired':
            policy.deadline = 0
        return ChatResponse([ThinkingBlock(thinking='private planning')], True)
    with pytest.raises(Exception) as caught:
        await collect(policy, model)
    assert caught.value.error_code in {'WORKER_EMPTY_RESPONSE', 'WORKER_TIMEOUT'}
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_empty_final_after_public_text_does_not_repeat_output():
    calls = []
    async def model(**kwargs):
        calls.append(kwargs)
        async def stream():
            yield ChatResponse([TextBlock(text='已公开内容')], False)
            yield ChatResponse([ThinkingBlock(thinking='private planning')], True)
        return stream()
    with pytest.raises(Exception) as caught:
        await collect(BankModelReliability(10), model)
    assert caught.value.error_code == 'WORKER_EMPTY_RESPONSE'
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('block', [TextBlock(text='正常答复'), ToolCallBlock(id='valid', name='read_range', input='{}')])
async def test_valid_response_does_not_consume_empty_recovery(block):
    calls = []
    async def model(**kwargs):
        calls.append(kwargs)
        return ChatResponse([block], True)
    policy = BankModelReliability(10)
    chunks = await collect(policy, model)
    assert chunks[-1].content == [block]
    assert len(calls) == 1 and policy.recovery_used is False


@pytest.mark.asyncio
@pytest.mark.parametrize('answer', [True, False])
async def test_actual_sdk_thinking_only_stream_never_finishes_without_valid_output(monkeypatch, answer):
    from types import SimpleNamespace
    from agentscope.credential._openai import OpenAICredential
    from agentscope.message import SystemMsg
    from qwenpaw.providers.openai_chat_model_compat import OpenAIChatModelCompat
    requests, closed = [], []
    class WireStream:
        def __init__(self, items): self.items = iter(items)
        async def __aenter__(self): return self
        async def __aexit__(self, *args): closed.append(True)
        def __aiter__(self): return self
        async def __anext__(self):
            try: return next(self.items)
            except StopIteration: raise StopAsyncIteration
    def chunk(content=None, thinking=None, finish=None):
        return SimpleNamespace(usage=None, choices=[SimpleNamespace(finish_reason=finish,
            delta=SimpleNamespace(content=content, reasoning_content=thinking, tool_calls=None))])
    async def api(**kwargs):
        requests.append(kwargs)
        assert kwargs['messages'][0]['role'] == 'system'
        assert all(m['role'] != 'system' for m in kwargs['messages'][1:])
        content = '有效答复' if len(requests) == 2 and answer else None
        return WireStream([chunk(thinking='private planning'), chunk(content=content), chunk(finish='stop')])
    monkeypatch.setattr('openai.AsyncClient', lambda **kwargs:
        SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=api))))
    model = OpenAIChatModelCompat(credential=OpenAICredential(id='probe', api_key='unused',
        base_url='http://unused.invalid'), model='nvidia/nemotron-3-super-120b-a12b:free', stream=True, max_retries=0)
    policy = BankModelReliability(10)
    request = {'messages': [SystemMsg('system', 'base'), UserMsg('user', '完成本轮任务')]}
    if answer:
        chunks = await collect(policy, model, **request)
        assert ''.join(b.text for b in chunks[-1].content if isinstance(b, TextBlock)) == '有效答复'
    else:
        with pytest.raises(Exception) as caught:
            await collect(policy, model, **request)
        assert caught.value.error_code == 'WORKER_EMPTY_RESPONSE'
    assert len(requests) == len(closed) == 2
    assert len(request['messages']) == 2
