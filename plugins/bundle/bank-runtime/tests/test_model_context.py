import copy
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
from agentscope.formatter import OpenAIChatFormatter
from agentscope.message import AssistantMsg, SystemMsg, TextBlock, ThinkingBlock, ToolCallBlock, ToolResultBlock, ToolResultState, UserMsg
from agentscope.model import ChatResponse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bank_runtime.model_context import prepare_public_model_context
from bank_runtime.gateway.middleware import BankRuntimeGatewayMiddleware
from bank_runtime.artifact_tools import ArtifactDeliveryIntent


@pytest.mark.asyncio
async def test_document_schema_guidance_precedes_user_facing_answer_guidance():
    middleware = BankRuntimeGatewayMiddleware(None, artifact_intent=ArtifactDeliveryIntent(
        "generate", "docx", layout_kind="official_document", layout_resolution="skill"
    ))
    request = {"messages": [UserMsg("user", "生成通知")], "tools": []}
    before = copy.deepcopy(request)

    async def model(**kwargs):
        return kwargs

    prepared = await middleware.on_model_call(None, request, model)
    assert request == before
    texts = [msg.get_text_content() for msg in prepared["messages"]]
    assert "本轮回答约定" in texts[-1]
    assert "参数说明" in texts[-1]
    assert "文件已生成，可在文件卡片中打开或下载。" in texts[-1]
    assert any("bank-official-docx-v1" in text for text in texts[:-1])
    assert any("delivery_plan" in text for text in texts[:-1])


@pytest.mark.asyncio
async def test_old_tool_context_is_projected_without_changing_evidence_or_current_turn():
    history = [SystemMsg("system", "base"), UserMsg("user", "历史上的今天"),
               AssistantMsg("assistant", [ThinkingBlock(thinking="Switch to shell after browser denial"),
                   ToolCallBlock(id="c1", name="Skill", input='{"skill":"browser"}'),
                   ToolResultBlock(id="c1", name="Skill", state=ToolResultState.DENIED,
                                   output="Runtime Tool Gateway preflight denied this call"),
                   TextBlock(text="历史上的今天说明")]),
               UserMsg("user", "顺德天气"),
               AssistantMsg("assistant", [ThinkingBlock(thinking="current signed reasoning")])]
    before = copy.deepcopy(history)
    request = {"messages": history, "tools": []}
    prepared = prepare_public_model_context(request)
    assert history == before
    old = prepared["messages"][2]
    assert not any(isinstance(block, ThinkingBlock) for block in old.content)
    assert old.content[0] == history[2].content[1]
    assert old.content[1].state == ToolResultState.DENIED
    assert old.content[1].id == "c1"
    assert "历史结果" in old.content[1].output[0].text
    assert prepared["messages"][-2] is history[-1]
    wire = await OpenAIChatFormatter().format(prepared["messages"])
    assert "本轮回答约定" in str(wire[-1]["content"])
    assert "Switch to shell" not in str(wire)
    assert "preflight denied" not in str(wire)


@pytest.mark.asyncio
async def test_ordinary_answer_stream_is_returned_without_inspection_buffering_or_rewrite():
    middleware = BankRuntimeGatewayMiddleware(None)
    middleware.allowed_tool_names = frozenset({"get_current_time"})
    consumed, calls = [], []
    async def stream():
        consumed.append(True)
        yield ChatResponse(content=[TextBlock(text="Runtime 是运行时。")], is_last=False)
        raise AssertionError("test must not consume beyond the first chunk")
    output = stream()
    async def model(**kwargs):
        calls.append(kwargs)
        return output
    result = await middleware.on_model_call(SimpleNamespace(), {
        "messages": [UserMsg("user", "解释 Runtime")],
        "tools": [{"type": "function", "function": {"name": name}} for name in ("get_current_time", "execute_shell_command")],
    }, model)
    assert result is output
    assert consumed == []
    assert len(calls) == 1
    assert [s["function"]["name"] for s in calls[0]["tools"]] == ["get_current_time"]
    assert (await anext(result)).content[0].text == "Runtime 是运行时。"
    await result.aclose()


def test_request_and_business_history_are_preserved():
    user = UserMsg("user", "解释 browser 和 Runtime 的关系")
    answer = AssistantMsg("assistant", "browser 是技能，文件名 report.md。")
    request = {"messages": [user, answer, UserMsg("user", "继续解释")], "tools": []}
    prepared = prepare_public_model_context(request)
    assert prepared["messages"][1] is user
    assert prepared["messages"][2].content == answer.content
    assert len(request["messages"]) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize('question,answer', [
    ('历史上的今天', '可以根据已知历史介绍对应日期的事件。'),
    ('今天的天气怎么样', '暂时没有当天实况，可以介绍当地气候作为背景。'),
    ('帮我写一段会议开场白', '各位同事，大家好。'),
])
async def test_no_tools_does_not_block_model_only_answers_after_old_refusal(question, answer):
    middleware = BankRuntimeGatewayMiddleware(None)
    middleware.allowed_tool_names = frozenset()
    calls = []
    response = ChatResponse(content=[TextBlock(text=answer)], is_last=True)
    async def model(**kwargs):
        calls.append(kwargs)
        return response
    user = UserMsg('user', question)
    result = await middleware.on_model_call(SimpleNamespace(), {
        'messages': [UserMsg('user', '此前的问题'),
                     AssistantMsg('assistant', '无法联网，因此不能回答。'), user],
        'tools': [],
    }, model)
    assert result is response
    assert len(calls) == 1
    assert calls[0]['tools'] == []
    assert user in calls[0]['messages']
    reminder = calls[0]['messages'][-1].get_text_content()
    assert '不以拥有对应工具或联网为前提' in reminder
    assert '不代表本轮已尝试' in reminder


@pytest.mark.asyncio
@pytest.mark.parametrize('layout,completed_read_repeats', [
    (None, False), (None, True), ('standard_document', False), ('official_document', False)])
@pytest.mark.parametrize('model_id', ['deepseek-v4-flash', 'qwen3-27b'])
async def test_real_bank_context_reaches_strict_provider_with_one_leading_system(monkeypatch, layout, model_id, completed_read_repeats):
    from unittest.mock import AsyncMock
    from agentscope.credential._openai import OpenAICredential
    from openai.types.chat import ChatCompletion
    from qwenpaw.providers.openai_chat_model_compat import OpenAIChatModelCompat
    intent = None if layout is None else ArtifactDeliveryIntent(
        'generate', 'docx', layout_kind=layout, layout_resolution='skill')
    middleware = BankRuntimeGatewayMiddleware(None, artifact_intent=intent)
    if layout is None:
        from test_document_reads_v2 import observe_parse, aggregate_evidence, repeat_raw_range
        observe_parse(middleware.document_reads)
        for _ in range(5):
            aggregate_evidence(middleware.document_reads)
        if completed_read_repeats:
            repeat_raw_range(middleware.document_reads, requested_end=5)
    history = [SystemMsg('system', 'base policy'), UserMsg('user', '分析Excel'),
               AssistantMsg('assistant', '先前未完成'), UserMsg('user', '可以生成一份docx文件给我吗')]
    before = copy.deepcopy(history)
    response = ChatCompletion(id='test', created=0, model='strict-test', object='chat.completion',
        choices=[{'index': 0, 'finish_reason': 'stop',
                  'message': {'role': 'assistant', 'content': '测试回答'}}])
    seen = []
    async def strict_api(*, messages, **kwargs):
        assert messages[0]['role'] == 'system'
        assert all(m['role'] != 'system' for m in messages[1:])
        assert '本轮回答约定' in str(messages[0]['content'])
        if layout:
            assert 'delivery_plan' in str(messages[0]['content'])
        else:
            assert '直接复用' in str(messages[0]['content'])
            if completed_read_repeats:
                assert '同一明确行范围已经完整返回' in str(messages[0]['content'])
                assert not kwargs.get('tools')
        seen.append(messages)
        return response
    monkeypatch.setattr('openai.AsyncClient', lambda **kwargs: SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(side_effect=strict_api)))))
    model = OpenAIChatModelCompat(
        credential=OpenAICredential(id='strict-probe', api_key='unused', base_url='http://127.0.0.1:1/v1'),
        model=model_id, stream=False)
    async def call(**kwargs):
        return await model._call_api(model_id, kwargs['messages'], kwargs.get('tools'))
    result = await middleware.on_model_call(None, {'messages': history, 'tools': []}, call)
    assert result.content[0].text == '测试回答'
    assert history == before
    assert len(seen) == 1
    assert [m['role'] for m in seen[0]] == ['system', 'user', 'assistant', 'user']


@pytest.mark.asyncio
@pytest.mark.parametrize('model_id', ['deepseek-v4-flash', 'qwen3-27b'])
async def test_followup_contract_reaches_current_model_request_after_history(model_id):
    from qwenpaw.agents.model_factory import _create_formatter_instance
    request = {'messages': [SystemMsg('system', 'base'), UserMsg('user', '分析需求收集流程'),
        AssistantMsg('assistant', '已有分析。需要我继续吗？'), UserMsg('user', '如何改进优先级标准？')], 'tools': []}
    before = copy.deepcopy(request)
    prepared = prepare_public_model_context(request)
    reminder = prepared['messages'][-1].get_text_content()
    assert '<bank_followups>' in reminder
    assert '用户视角' in reminder
    assert '没有合适追问' in reminder
    assert '正文末尾' in reminder
    assert request == before
    from qwenpaw.providers.openai_provider import OpenAIProvider
    model = OpenAIProvider(id='followup-test', name='test', api_key='unused', base_url='http://test.invalid/v1').get_chat_model_instance(model_id)
    formatter = _create_formatter_instance(model, provider_id='followup-test')
    wire = await formatter.format(prepared['messages'])
    # Native model order normalization retains the current contract, never
    # elevates the user message or rewrites the persisted conversation.
    from qwenpaw.providers.chat_message_order import normalize_system_messages
    normalized = normalize_system_messages(wire)
    assert [m['role'] for m in normalized] == ['system', 'user', 'assistant', 'user']
    assert '<bank_followups>' in str(normalized[0]['content'])
    assert normalized[-1]['content'] == wire[-2]['content']
