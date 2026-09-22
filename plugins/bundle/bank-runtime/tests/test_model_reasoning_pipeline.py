"""Exercise actual model parsing, block events, Envelope and public SSE together."""
import json
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from agentscope.agent import Agent
from agentscope.credential import OpenAICredential
from agentscope.event import TextBlockEndEvent, ThinkingBlockEndEvent
from qwenpaw.providers.openai_chat_model_compat import OpenAIChatModelCompat
from qwenpaw.runtime.envelope import Envelope

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bank_runtime.events import project_sse_stream


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


class Model(OpenAIChatModelCompat):
    async def _call_api(self, *args, **kwargs):
        return self._parse_stream_response(datetime.now(), self.wire)


@pytest.mark.asyncio
@pytest.mark.parametrize("parts", [
    [("分析", "前半段"), (None, "后半段")],
    [("分析", "前半段后半段")],
    [(None, "<think>分析</think>前半段后半段")],
    [(None, "分析</thi"), (None, "nk>前半段"), (None, "后半段")],
])
async def test_real_model_history_and_public_stream_keep_the_whole_answer(parts):
    chunks = [NS(usage=None, choices=[NS(delta=NS(reasoning_content=r, content=t, tool_calls=None),
                                        finish_reason=None)]) for r, t in parts]
    chunks.append(NS(usage=None, choices=[NS(delta=NS(content=None, reasoning_content=None, tool_calls=None),
                                            finish_reason="stop")]))
    model = Model(model="deepseek-v4-flash", stream=True,
                  credential=OpenAICredential(api_key="synthetic", base_url="http://unused.invalid"))
    object.__setattr__(model, "wire", WireStream(chunks))
    env = Envelope("synthetic")
    block_ids = {"tools": [], "data": []}
    agent = NS(state=NS(reply_id="synthetic"))
    history = []

    async def source():
        stream = await model(messages=[])
        async for response in stream:
            if response.is_last:
                history.extend(response.content)
                continue
            async for event in Agent._convert_chat_response_to_event(agent, block_ids, response):
                async for obj in env.translate_event(event):
                    yield "data: " + obj.model_dump_json() + "\n\n"
        # Same end order as AgentScope's model-call lifecycle.
        for key, cls in [("text", TextBlockEndEvent), ("thinking", ThinkingBlockEndEvent)]:
            if block_ids.get(key):
                async for obj in env.translate_event(cls(reply_id="synthetic", block_id=block_ids[key])):
                    yield "data: " + obj.model_dump_json() + "\n\n"
        async for obj in env.finalize():
            yield "data: " + obj.model_dump_json() + "\n\n"

    events = [json.loads(e.removeprefix("data: ")) async for e in project_sse_stream(source(), "task")]
    assert "".join(e["text"] for e in events if e["event"] == "answer.chunk") == "前半段后半段"
    assert not any(e["event"] == "answer.phase" for e in events)
    assert "".join(getattr(b, "text", "") for b in history) == "前半段后半段"
    assert events[-1]["event"] == "answer.completed"
