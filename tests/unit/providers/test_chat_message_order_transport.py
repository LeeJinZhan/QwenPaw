"""Real SDK encoding and wrapper regressions using an offline HTTP transport."""
import copy
import json

import httpx
import pytest
from agentscope.message import AssistantMsg, SystemMsg, UserMsg, ToolCallBlock, ToolResultBlock, ToolResultState
from qwenpaw.agents.model_factory import _create_formatter_instance
from qwenpaw.providers.openai_provider import OpenAIProvider
from qwenpaw.providers.retry_chat_model import RetryChatModel, RetryConfig, RateLimitConfig
from qwenpaw.token_usage.model_wrapper import TokenRecordingModelWrapper


@pytest.mark.asyncio
@pytest.mark.parametrize('model_id', ['qwen3-27b', 'deepseek-v4-flash'])
@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('retry_mode', ['none', 'server_error', 'reasoning'])
async def test_full_wrapper_chain_and_real_sdk_http_body(model_id, stream, retry_mode):
    seen = []
    async def handle(request):
        wire = json.loads(request.content)
        seen.append(wire)
        assert [m['role'] for m in wire['messages']] == ['system', 'user', 'assistant', 'tool']
        assert wire['messages'][2]['tool_calls'][0]['id'] == wire['messages'][3]['tool_call_id'] == 'call1'
        assert 'base' in str(wire['messages'][0]['content'])
        assert 'current turn' in str(wire['messages'][0]['content'])
        assert wire['tools'][0]['function']['name'] == 'audit_tool'
        assert wire['stream'] is stream
        if len(seen) == 1 and retry_mode != 'none':
            code = 400 if retry_mode == 'reasoning' else 500
            message = 'Missing reasoning_content on assistant message' if code == 400 else 'temporary server error'
            return httpx.Response(code, json={'error': {'message': message, 'type': 'invalid_request_error'}})
        if retry_mode == 'reasoning':
            assert wire['messages'][2]['reasoning_content'].strip() == ''
        if stream:
            events = []
            for delta, finish in [({'role': 'assistant', 'content': 'audit passed'}, None), ({}, 'stop')]:
                events.append('data: ' + json.dumps({'id': 'audit', 'object': 'chat.completion.chunk', 'created': 0,
                    'model': model_id, 'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]}))
            return httpx.Response(200, headers={'content-type': 'text/event-stream'},
                content=('\n\n'.join(events) + '\n\ndata: [DONE]\n\n').encode())
        return httpx.Response(200, json={'id': 'audit', 'object': 'chat.completion', 'created': 0,
            'model': model_id, 'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'audit passed'}, 'finish_reason': 'stop'}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        provider_id = f'audit-{model_id}-{stream}-{retry_mode}'
        provider = OpenAIProvider(id=provider_id, name='audit', api_key='unused', base_url='http://audit.invalid/v1')
        core = provider.get_chat_model_instance(model_id)
        core.stream = stream
        core.client_kwargs = {'http_client': client, 'max_retries': 0}
        core.max_retries = 0
        core.formatter = _create_formatter_instance(core, provider_id=provider_id)
        original_formatter = core.formatter
        wrapped = RetryChatModel(TokenRecordingModelWrapper(provider_id, core),
            retry_config=RetryConfig(enabled=True, max_retries=1, backoff_base=0.01, backoff_cap=0.01),
            rate_limit_config=RateLimitConfig(max_qpm=0))
        messages = [SystemMsg('system', 'base'), UserMsg('user', 'word please'),
            AssistantMsg('assistant', [ToolCallBlock(id='call1', name='audit_tool', input='{}')]),
            SystemMsg('system', 'current turn'),
            AssistantMsg('assistant', [ToolResultBlock(id='call1', name='audit_tool', output='done', state=ToolResultState.SUCCESS)])]
        before = copy.deepcopy(messages)
        response = await wrapped(messages=messages, tools=[{'type': 'function', 'function': {
            'name': 'audit_tool', 'description': 'synthetic test', 'parameters': {'type': 'object', 'properties': {}}}}])
        if stream:
            responses = [part async for part in response]
            assert responses[-1].is_last
            assert responses[-1].content[0].text == 'audit passed'
        else:
            assert response.content[0].text == 'audit passed'
        assert len(seen) == (1 if retry_mode == 'none' else 2)
        assert messages == before
        assert core.formatter is original_formatter


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["messages", "model", "stream", "tools", "tool_choice"])
@pytest.mark.parametrize("source", ["provider", "call", "model"])
async def test_extra_body_cannot_override_managed_request_fields(field, source):
    requests = []
    async def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"id": "audit", "object": "chat.completion", "created": 0,
            "model": "qwen3-27b", "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]})
    values = {"messages": [{"role": "user", "content": "overridden"},
                           {"role": "system", "content": "late policy"}],
              "model": "other-model", "stream": False, "tools": [], "tool_choice": "none"}
    extension = {field: values[field]}
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        provider = OpenAIProvider(id="extra-body-audit", name="audit", api_key="unused",
            base_url="http://audit.invalid/v1",
            generate_kwargs={"extra_body": extension} if source == "provider" else {})
        core = provider.get_chat_model_instance("qwen3-27b")
        core.stream = False
        core.max_retries = 0
        core.client_kwargs = {"http_client": client, "max_retries": 0}
        core.formatter = _create_formatter_instance(core, provider_id=provider.id)
        if source == "model":
            core.extra_body = extension
        before = copy.deepcopy(extension)
        with pytest.raises(ValueError, match="extra_body.*" + field):
            await core(messages=[SystemMsg("system", "base"), UserMsg("user", "original")],
                       **({"extra_body": extension} if source == "call" else {}))
        assert extension == before
    assert requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("model_id", ["qwen3-27b", "deepseek-v4-flash"])
async def test_extra_body_keeps_model_specific_generation_parameters(model_id):
    bodies = []
    async def handle(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "audit", "object": "chat.completion", "created": 0,
            "model": model_id, "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]})
    extension = {"chat_template_kwargs": {"enable_thinking": False}, "top_k": 20,
                 "thinking": {"type": "disabled"}}
    before = copy.deepcopy(extension)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        provider = OpenAIProvider(id="generation-audit", name="audit", api_key="unused",
            base_url="http://audit.invalid/v1", generate_kwargs={"extra_body": extension})
        core = provider.get_chat_model_instance(model_id)
        core.stream = False
        core.client_kwargs = {"http_client": client, "max_retries": 0}
        core.formatter = _create_formatter_instance(core, provider_id=provider.id)
        response = await core(messages=[SystemMsg("system", "base"), UserMsg("user", "original"),
                                        SystemMsg("system", "last instruction")])
        assert response.content[0].text == "ok"
    assert [m["role"] for m in bodies[0]["messages"]] == ["system", "user"]
    assert all(bodies[0][key] == value for key, value in extension.items())
    assert extension == before
