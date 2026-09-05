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
