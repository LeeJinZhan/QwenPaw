import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.mark.parametrize('model_name', ['deepseek-v4-flash', 'qwen3-27b'])
@pytest.mark.asyncio
async def test_review_is_single_system_no_tools_and_bounded(monkeypatch, model_name):
    from bank_runtime.content_review import ReviewRequest, review_content
    seen = []
    class Model:
        model = model_name
        async def __call__(self, messages, **kwargs):
            seen.append((messages, kwargs))
            return {'content': [{'type': 'text', 'text': json.dumps({'classification': 'analysis', 'risk_codes': [], 'evidence': []})}]}
    monkeypatch.setattr('bank_runtime.content_review.create_review_model', lambda _: Model())
    result = await review_content('assistant-a', ReviewRequest(text='分析：禁止保证收益。'))
    assert result['classification'] == 'analysis'
    messages, kwargs = seen[0]
    assert [message.role for message in messages] == ['system', 'user']
    assert kwargs['tools'] is None
    assert kwargs['max_tokens'] == 512
    assert kwargs['stream'] is False
    assert '禁止保证收益' in str(messages[1].content)


def test_review_route_requires_matching_service_identity(monkeypatch):
    from bank_runtime.content_review import build_content_review_router
    monkeypatch.setenv('QWENPAW_SERVICE_TOKEN', 'review-service-secret')
    monkeypatch.setattr('bank_runtime.content_review.require_production_readiness', lambda: None)
    app = FastAPI()
    app.include_router(build_content_review_router(), prefix='/api/bank-runtime')
    client = TestClient(app)
    path = '/api/bank-runtime/agents/assistant-a/content-review'
    assert client.post(path, json={'text': '分析保证收益'}).status_code == 401
    assert client.post(path, json={'text': '分析保证收益'}, headers={'Authorization': 'Bearer review-service-secret', 'X-Agent-Id': 'assistant-b'}).status_code == 401


def test_review_cold_agent_does_not_initialize_workspace_or_tools(monkeypatch):
    from bank_runtime.content_review import build_content_review_router
    monkeypatch.setenv('QWENPAW_SERVICE_TOKEN', 'review-service-secret')
    monkeypatch.setattr('bank_runtime.content_review.require_production_readiness', lambda: None)
    monkeypatch.setattr('bank_runtime.content_review.load_config', lambda: SimpleNamespace(
        agents=SimpleNamespace(profiles={'assistant-a': SimpleNamespace(enabled=True)})))
    async def classify(*_):
        return {'classification': 'analysis', 'risk_codes': [], 'evidence': []}
    monkeypatch.setattr('bank_runtime.content_review.review_content', classify)
    app = FastAPI()  # No MultiAgentManager or Workspace is constructed.
    app.include_router(build_content_review_router(), prefix='/api/bank-runtime')
    response = TestClient(app).post('/api/bank-runtime/agents/assistant-a/content-review',
        json={'text': '分析保证收益'}, headers={'Authorization': 'Bearer review-service-secret', 'X-Agent-Id': 'assistant-a'})
    assert response.status_code == 200
    assert response.json()['classification'] == 'analysis'


@pytest.mark.parametrize('profiles', [{}, {'assistant-a': SimpleNamespace(enabled=False)}])
def test_review_agent_registry_rejects_missing_or_disabled(monkeypatch, profiles):
    from bank_runtime.content_review import require_review_agent
    monkeypatch.setattr('bank_runtime.content_review.load_config', lambda: SimpleNamespace(
        agents=SimpleNamespace(profiles=profiles)))
    with pytest.raises(ValueError):
        require_review_agent('assistant-a')


@pytest.mark.asyncio
@pytest.mark.parametrize('with_text', [False, True])
async def test_review_does_not_accept_tool_calls_or_invalid_json(monkeypatch, with_text):
    from bank_runtime.content_review import ReviewRequest, review_content
    class Model:
        async def __call__(self, messages, **kwargs):
            return {'content': ([{'type': 'text', 'text': '{"classification":"analysis","risk_codes":[],"evidence":[]}'}] if with_text else []) + [{'type': 'tool_use', 'name': 'shell.exec'}]}
    monkeypatch.setattr('bank_runtime.content_review.create_review_model', lambda _: Model())
    with pytest.raises(ValueError):
        await review_content('assistant-a', ReviewRequest(text='分析保证收益'))


@pytest.mark.asyncio
@pytest.mark.parametrize('model_id', ['qwen3-27b', 'deepseek-v4-flash'])
@pytest.mark.parametrize('output_param', ['max_tokens', 'max_completion_tokens'])
async def test_review_actual_sdk_wire_keeps_budget_order_and_no_tools(monkeypatch, model_id, output_param):
    from agentscope.credential import OpenAICredential
    from qwenpaw.providers.openai_chat_model_compat import OpenAIChatModelCompat
    from bank_runtime.content_review import ReviewRequest, review_content
    captured = []
    class RequestCaptured(Exception):
        pass
    async def create(**kwargs):
        captured.append(kwargs)
        raise RequestCaptured
    monkeypatch.setattr('openai.AsyncClient', lambda **_: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    model = OpenAIChatModelCompat(credential=OpenAICredential(id='review-wire-probe', api_key='unused', base_url='http://127.0.0.1:1/v1'),
        model=model_id, stream=False, output_token_param=output_param, extra_generate_kwargs={output_param: 8192})
    monkeypatch.setattr('bank_runtime.content_review.create_review_model', lambda _: model)
    with pytest.raises(RequestCaptured):
        await review_content('assistant-a', ReviewRequest(text='分析：禁止保证收益。'))
    wire = captured[0]
    assert [message['role'] for message in wire['messages']] == ['system', 'user']
    assert all(isinstance(message['content'], str) for message in wire['messages'])
    assert wire.get('tools') in (None, [])
    assert wire.get('tool_choice') in (None, 'none')
    assert wire[output_param] == 512 and wire['stream'] is False
    assert wire['extra_body']['enable_thinking'] is False
    assert wire['extra_body']['thinking'] == {'type': 'disabled'}


@pytest.mark.asyncio
async def test_review_factory_parses_non_stream_reply_from_streaming_provider(monkeypatch):
    from agentscope.credential import OpenAICredential
    from openai.types.chat import ChatCompletion
    from qwenpaw.providers.openai_chat_model_compat import OpenAIChatModelCompat
    from bank_runtime.content_review import ReviewRequest, review_content
    captured = []
    async def create(**kwargs):
        captured.append(kwargs)
        return ChatCompletion(id='review-reply', created=0, model='qwen3-27b', object='chat.completion',
            choices=[{'index': 0, 'finish_reason': 'stop', 'message': {'role': 'assistant',
                'content': '{"classification":"analysis","risk_codes":[],"evidence":[]}'}}],
            usage={'prompt_tokens': 10, 'completion_tokens': 10, 'total_tokens': 20})
    monkeypatch.setattr('openai.AsyncClient', lambda **_: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    shared = OpenAIChatModelCompat(credential=OpenAICredential(id='review-parser-probe', api_key='unused', base_url='http://127.0.0.1:1/v1'),
        model='qwen3-27b', stream=True, extra_generate_kwargs={'max_tokens': 8192,
            'tools': [{'type': 'function', 'function': {'name': 'foreign_tool'}}], 'tool_choice': 'auto'})
    manager = SimpleNamespace(get_provider=lambda _: SimpleNamespace(get_chat_model_instance=lambda _: shared))
    monkeypatch.setattr('qwenpaw.providers.ProviderManager.get_instance', lambda: manager)
    monkeypatch.setattr('qwenpaw.config.config.load_agent_config', lambda _: SimpleNamespace(
        active_model=SimpleNamespace(provider_id='provider-a', model='qwen3-27b')))
    result = await review_content('assistant-a', ReviewRequest(text='分析：禁止保证收益。'))
    assert result['classification'] == 'analysis'
    assert captured[0]['stream'] is False and 'stream_options' not in captured[0]
    assert captured[0]['max_tokens'] == 512
    assert captured[0].get('tools') in (None, []) and captured[0].get('tool_choice') in (None, 'none')
    assert shared.stream is True and shared._extra_generate_kwargs['max_tokens'] == 8192
    assert shared._extra_generate_kwargs['tool_choice'] == 'auto'
