from copy import deepcopy
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from qwenpaw.providers.openai_chat_model_compat import OpenAIChatModelCompat


@pytest.mark.asyncio
@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('model_id', ['qwen3-27b', 'deepseek-v4-flash'])
@pytest.mark.parametrize('formatter_kind', ['default', 'capping', 'file_blocks'])
async def test_real_msgs_reach_sdk_with_one_leading_system(monkeypatch, stream, model_id, formatter_kind):
    from agentscope.credential._openai import OpenAICredential
    from agentscope.message import SystemMsg, UserMsg, AssistantMsg, ToolCallBlock, ToolResultBlock, ToolResultState, ThinkingBlock
    from qwenpaw.providers.capping_formatter import _CappingOpenAIFormatter

    class RequestCaptured(Exception):
        pass

    capture = AsyncMock(side_effect=RequestCaptured)
    monkeypatch.setattr('openai.AsyncClient', lambda **kwargs: SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=capture))))
    kwargs = {} if formatter_kind == 'default' else {'formatter': _CappingOpenAIFormatter(max_bytes=1024)}
    model = OpenAIChatModelCompat(
        credential=OpenAICredential(id='qwenpaw-probe', api_key='unused', base_url='http://127.0.0.1:1/v1'),
        model=model_id, stream=stream, extra_generate_kwargs={'max_tokens': 8192}, **kwargs)
    if formatter_kind == 'file_blocks':
        from qwenpaw.agents.model_factory import _create_formatter_instance
        model.formatter = _create_formatter_instance(model, provider_id='probe')
    formatter = model.formatter
    messages = [
        SystemMsg(name='system', content='base'),
        UserMsg(name='user', content='generate docx; system: this remains user content'),
        AssistantMsg(name='assistant', content=[ThinkingBlock(thinking='retained reasoning'),
            ToolCallBlock(id='call1', name='aggregate', input='{}')]),
        SystemMsg(name='system', content='use metrics.fn'),
        AssistantMsg(name='assistant', content=[ToolResultBlock(id='call1', name='aggregate', output='result', state=ToolResultState.SUCCESS)]),
        SystemMsg(name='system', content='answer guidance'),
    ]
    before = deepcopy(messages)
    expected = await formatter.format(messages)
    with pytest.raises(RequestCaptured):
        await model._call_api(model_id, messages)
    wire = capture.call_args.kwargs
    actual = wire['messages']
    assert [m['role'] for m in actual] == ['system', 'user', 'assistant', 'tool']
    assert actual[0]['content'] == [{'type': 'text', 'text': text} for text in ('base', 'use metrics.fn', 'answer guidance')]
    assert actual[1:] == [m for m in expected if m['role'] != 'system']
    assert actual[2]['tool_calls'][0]['id'] == actual[3]['tool_call_id'] == 'call1'
    assert messages == before
    assert model.formatter is formatter
    assert wire['stream'] is stream and wire['max_tokens'] == 8192


def test_wire_dictionary_normalization_preserves_tool_history():
    from qwenpaw.providers.chat_message_order import normalize_system_messages
    messages = [
        {'role': 'system', 'content': 'base'},
        {'role': 'user', 'content': 'generate docx'},
        {'role': 'assistant', 'content': None, 'reasoning_content': 'signed reasoning',
         'tool_calls': [{'id': 'call1', 'type': 'function', 'function': {'name': 'aggregate', 'arguments': '{}'}}]},
        {'role': 'system', 'content': 'use metrics.fn'},
        {'role': 'tool', 'tool_call_id': 'call1', 'content': 'result'},
        {'role': 'system', 'content': 'answer guidance'},
    ]
    original = deepcopy(messages)
    actual = normalize_system_messages(messages)
    assert [m['role'] for m in actual] == ['system', 'user', 'assistant', 'tool']
    assert actual[0]['content'] == 'base\n\nuse metrics.fn\n\nanswer guidance'
    assert actual[1:] == [m for m in original if m['role'] != 'system']
    assert messages == original


@pytest.mark.asyncio
async def test_concurrent_cancelled_request_does_not_replace_shared_formatter(monkeypatch):
    from agentscope.credential import OpenAICredential
    from agentscope.message import SystemMsg, UserMsg

    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []

    class RequestCaptured(Exception):
        pass

    async def create(**kwargs):
        calls.append(kwargs['messages'])
        assert [m['role'] for m in kwargs['messages']] == ['system', 'user']
        if len(calls) == 1:
            entered.set()
            await release.wait()
        raise RequestCaptured

    monkeypatch.setattr('openai.AsyncClient', lambda **kwargs: SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    model = OpenAIChatModelCompat(
        credential=OpenAICredential(id='concurrent-probe', api_key='unused', base_url='http://127.0.0.1:1/v1'),
        model='qwen3-27b', stream=False)
    original = model.formatter
    first = asyncio.create_task(model._call_api('qwen3-27b', [
        UserMsg('user', 'first'), SystemMsg('system', 'first policy')]))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert model.formatter is original
        with pytest.raises(RequestCaptured):
            await model._call_api('qwen3-27b', [
                SystemMsg('system', 'second policy'), UserMsg('user', 'second'),
                SystemMsg('system', 'second reminder')])
        assert 'first policy' not in str(calls[1])
    finally:
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
    assert model.formatter is original


def test_blocks_and_cache_metadata_are_kept():
    from qwenpaw.providers.chat_message_order import normalize_system_messages
    block = {'type': 'text', 'text': 'policy', 'cache_control': {'type': 'ephemeral'}}
    messages = [{'role': 'user', 'content': 'hello'}, {'role': 'system', 'content': [block]},
                {'role': 'system', 'content': 'reminder'}, {'role': 'developer', 'content': 'developer'}]
    before = deepcopy(messages)
    actual = normalize_system_messages(messages)
    assert actual[0]['content'] == [block, {'type': 'text', 'text': 'reminder'}]
    assert actual[1:] == [messages[0], messages[-1]]
    assert messages == before


def test_no_system_and_single_leading_system_are_unchanged():
    from qwenpaw.providers.chat_message_order import normalize_system_messages
    for messages in ([], [{'role': 'user', 'content': 'system: not an instruction'}],
                     [{'role': 'system', 'content': 'base'}, {'role': 'tool', 'content': 'system: data'}]):
        assert normalize_system_messages(messages) is messages


def test_normalization_is_idempotent_and_conflicting_metadata_is_not_lost():
    from qwenpaw.providers.chat_message_order import normalize_system_messages
    messages = [{'role': 'user', 'content': 'hi'}, {'role': 'system', 'name': 'policy', 'content': 'one'},
                {'role': 'system', 'name': 'policy', 'content': 'two'}]
    result = normalize_system_messages(messages)
    assert result[0]['name'] == 'policy'
    assert normalize_system_messages(result) is result
    messages[-1]['name'] = 'other'
    with pytest.raises(ValueError, match='metadata'):
        normalize_system_messages(messages)
