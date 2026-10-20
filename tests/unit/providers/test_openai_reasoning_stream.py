"""Public text must be classified before it enters AgentScope history/events."""
from datetime import datetime
from types import SimpleNamespace as NS

import pytest
from agentscope.credential import OpenAICredential
from qwenpaw.exceptions import ModelExecutionException
from qwenpaw.providers.openai_chat_model_compat import OpenAIChatModelCompat


class WireStream:
    def __init__(self, items):
        self.items = iter(items)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self.items)
        except StopIteration:
            raise StopAsyncIteration


def chunk(text=None, thinking=None, finish=None):
    return NS(usage=None, choices=[NS(delta=NS(content=text, reasoning_content=thinking,
                                             tool_calls=None), finish_reason=finish)])


def model(name="deepseek-v4-flash"):
    return OpenAIChatModelCompat(model=name, stream=True,
                                credential=OpenAICredential(api_key="synthetic", base_url="http://unused.invalid"))


async def collect(items, name="deepseek-v4-flash"):
    blocks = []
    async for response in model(name)._parse_stream_response(datetime.now(), WireStream(items)):
        blocks.extend(response.content)
    return ("".join(getattr(b, "text", "") for b in blocks),
            "".join(getattr(b, "thinking", "") for b in blocks))


@pytest.mark.asyncio
@pytest.mark.parametrize("parts", [
    ["<think>内部分析</think>正文"],
    list("<think>内部分析</think>正文"),
    ["内部分析", "</thi", "nk>", "正文"],
    ["<think>分析一</think><think>分析二</think>正文"],
])
async def test_inline_thinking_never_enters_public_text(parts):
    answer, thinking = await collect([*(chunk(p) for p in parts), chunk(finish="stop")])
    assert answer == "正文"
    assert "分析" in thinking
    assert "think>" not in thinking


@pytest.mark.asyncio
@pytest.mark.parametrize("parts", [[chunk("<think>未结束"), chunk(finish="stop")],
                                  [chunk("半截回答")],
                                  [chunk("半截回答"), chunk(finish="unknown")]])
async def test_unclosed_thinking_or_missing_finish_never_completes(parts):
    with pytest.raises(ModelExecutionException):
        await collect(parts)


@pytest.mark.asyncio
async def test_structured_reasoning_and_non_reasoning_text_still_stream():
    response = model()._parse_stream_response(datetime.now(), WireStream([
        chunk(thinking="分析"), chunk("正文"), chunk(finish="stop")]))
    assert (await anext(response)).content[0].thinking == "分析"
    assert (await anext(response)).content[0].text == "正文"
    await response.aclose()
    response = model("plain-model")._parse_stream_response(datetime.now(), WireStream([
        chunk("直接回答"), chunk(finish="stop")]))
    assert (await anext(response)).content[0].text == "直接回答"
    await response.aclose()


@pytest.mark.asyncio
async def test_omitted_opening_tag_is_buffered_before_any_public_text():
    response = model()._parse_stream_response(datetime.now(), WireStream([
        chunk("内部分析"), chunk("</think>正文"), chunk(finish="stop")]))
    first = await anext(response)
    assert not any(getattr(b, "text", "") == "内部分析" for b in first.content)
    await response.aclose()


@pytest.mark.asyncio
async def test_literal_tag_examples_in_code_are_preserved():
    text = "示例：`<think>文本</think>`。"
    assert await collect([chunk(text), chunk(finish="stop")]) == (text, "")


@pytest.mark.asyncio
async def test_terminal_only_reasoning_does_not_invent_an_answer():
    assert await collect([chunk("<think>分析</think>"), chunk(finish="stop")]) == ("", "分析")


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["```xml\n<think>内容</think>\n```", "示例：``<think>内容</think>``。", "比较 x <"])
async def test_code_delimiters_split_at_every_character_are_literal(text):
    assert await collect([*(chunk(c) for c in text), chunk(finish="stop")]) == (text, "")


@pytest.mark.asyncio
async def test_incompatible_streams_do_not_grow_an_unbounded_prefix_buffer():
    with pytest.raises(ModelExecutionException):
        await collect([chunk("x" * 1_000_001), chunk(finish="stop")])


@pytest.mark.asyncio
async def test_provider_metadata_and_tool_calls_survive_reasoning_classification():
    event = chunk("<think>分析</think>", finish="tool_calls")
    event.choices[0].delta.tool_calls = [NS(index=0, id="call_1", function=NS(name="lookup", arguments="{}"))]
    result = []
    async for response in model()._parse_stream_response(datetime.now(), WireStream([event])):
        result.extend(response.content)
    assert any(getattr(b, "name", "") == "lookup" for b in result)
    assert not any(getattr(b, "text", "") for b in result)
