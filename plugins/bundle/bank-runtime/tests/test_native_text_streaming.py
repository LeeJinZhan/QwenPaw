"""Timing regressions for the native QwenPaw-to-Runtime SSE boundary."""
import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bank_runtime.events import CompactEventProjector, project_sse_stream


def start(message_id="answer", kind="message"):
    return {"object": "message", "type": kind, "id": message_id,
            "status": "in_progress", "content": []}


def chunk(text, message_id="answer", delta=True):
    return {"object": "content", "type": "text", "msg_id": message_id,
            "delta": delta, "text": text}


@pytest.mark.asyncio
async def test_first_native_text_arrives_before_upstream_finishes():
    release = asyncio.Event()

    async def source():
        for event in [start(), chunk("第一段")]:
            yield "data: " + json.dumps(event) + "\n\n"
        await release.wait()
        for event in [chunk("第二段"), chunk("第一段第二段", delta=False),
                      {"object": "response", "status": "completed"}]:
            yield "data: " + json.dumps(event) + "\n\n"

    stream = project_sse_stream(source(), "task-stream")
    assert "status.changed" in await anext(stream)
    pending = asyncio.create_task(anext(stream))
    try:
        done, _ = await asyncio.wait({pending}, timeout=0.1)
        assert pending in done, "正文被缓存到了上游流结束"
        first = json.loads(pending.result().removeprefix("data: "))
        assert first == {"event": "answer.chunk", "text": "第一段", "message_id": "answer"}
        release.set()
        remaining = [json.loads(item.removeprefix("data: ")) async for item in stream]
        assert [item["text"] for item in remaining if item["event"] == "answer.chunk"] == ["第二段"]
        assert remaining[-1]["event"] == "answer.completed"
    finally:
        release.set()
        await pending
        await stream.aclose()


def test_classified_text_streams_and_moves_to_phase_at_tool_boundary():
    projector = CompactEventProjector("task")
    assert projector.project(start("preamble")) == []
    assert projector.project(chunk("正在查阅资料。", "preamble")) == [
        {"event": "answer.chunk", "text": "正在查阅资料。", "message_id": "preamble"}
    ]
    assert projector.project({"object": "message", "type": "plugin_call", "id": "tool"}) == [
        {"event": "answer.phase", "message_id": "preamble", "text": "正在查阅资料。"}
    ]
    assert projector.project(start("final")) == []
    assert projector.project(chunk("查阅结果。", "final"))[0]["text"] == "查阅结果。"
    assert projector.project({"object": "response", "status": "completed"}) == [
        {"event": "answer.completed", "status": "completed", "message": "回答完成"}
    ]


def test_reasoning_and_unclassified_text_do_not_become_live_answers():
    projector = CompactEventProjector("task")
    assert projector.project(chunk("尚未分类", "unknown")) == []
    assert projector.project(start("unknown", "reasoning")) == []
    thinking = projector.project(chunk("思考内容", "unknown"))
    assert [event["event"] for event in thinking] == ["answer.thinking"]
    failed = projector.project({"object": "response", "status": "failed"})
    assert [event["event"] for event in failed] == ["answer.failed"]


def test_cancel_does_not_replay_streamed_text_or_claim_success():
    projector = CompactEventProjector("task")
    projector.project(start())
    assert projector.project(chunk("未完成的正文"))[0]["event"] == "answer.chunk"
    failed = projector.project({"object": "response", "status": "cancelled"})
    assert [event["event"] for event in failed] == ["answer.failed"]
    assert failed[0]["error_code"] == "QWENPAW_TASK_CANCELLED"
    assert projector.project({"object": "response", "status": "completed"}) == []


@pytest.mark.asyncio
async def test_real_native_envelope_classifies_text_before_first_delta():
    from agentscope.event import TextBlockStartEvent, TextBlockDeltaEvent
    from qwenpaw.runtime.envelope import Envelope

    envelope = Envelope("stream-session")
    projector = CompactEventProjector("task")
    output = []
    for event in [TextBlockStartEvent(reply_id="reply", block_id="block"),
                  TextBlockDeltaEvent(reply_id="reply", block_id="block", delta="真实封装首段")]:
        async for native in envelope.translate_event(event):
            output.extend(projector.project(native.model_dump(mode="json")))
    assert len(output) == 1
    assert output[0]["event"] == "answer.chunk"
    assert output[0]["text"] == "真实封装首段"
    assert output[0]["message_id"]


@pytest.mark.asyncio
async def test_multiple_native_text_blocks_do_not_repeat_completed_snapshots():
    from agentscope.event import TextBlockStartEvent, TextBlockDeltaEvent, TextBlockEndEvent
    from qwenpaw.runtime.envelope import Envelope

    envelope = Envelope("multi-block-session")
    projector = CompactEventProjector("task")
    output = []
    for index, text in enumerate(["第一段", "第二段"]):
        args = {"reply_id": "reply", "block_id": str(index)}
        for event in [TextBlockStartEvent(**args), TextBlockDeltaEvent(**args, delta=text), TextBlockEndEvent(**args)]:
            async for native in envelope.translate_event(event):
                output.extend(projector.project(native.model_dump(mode="json")))
    output.extend(projector.project({"object": "response", "status": "completed"}))
    assert [item["text"] for item in output if item["event"] == "answer.chunk"] == ["第一段", "第二段"]
