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


@pytest.mark.asyncio
async def test_real_envelope_empty_text_header_before_tool_does_not_retire_final_body():
    from agentscope.event import TextBlockStartEvent, TextBlockDeltaEvent, TextBlockEndEvent, ToolCallStartEvent
    from qwenpaw.runtime.envelope import Envelope
    envelope=Envelope('empty-preamble');projector=CompactEventProjector('task');output=[]
    events=[TextBlockStartEvent(reply_id='reply',block_id='empty'),
            ToolCallStartEvent(reply_id='reply',tool_call_id='read',tool_call_name='read_range'),
            TextBlockStartEvent(reply_id='reply',block_id='final'),
            TextBlockDeltaEvent(reply_id='reply',block_id='final',delta='各表统计已返回，合计7条。'),
            TextBlockEndEvent(reply_id='reply',block_id='final')]
    for event in events:
        async for native in envelope.translate_event(event):
            output.extend(projector.project(native.model_dump(mode='json')))
    async for native in envelope.finalize():
        output.extend(projector.project(native.model_dump(mode='json')))
    assert ''.join(e['text'] for e in output if e['event']=='answer.chunk')=='各表统计已返回，合计7条。'
    assert output[-1]['event']=='answer.completed'


def test_cancel_does_not_replay_streamed_text_or_claim_success():
    projector = CompactEventProjector("task")
    projector.project(start())
    assert projector.project(chunk("未完成的正文"))[0]["event"] == "answer.chunk"
    failed = projector.project({"object": "response", "status": "cancelled"})
    assert [event["event"] for event in failed] == ["answer.failed"]
    assert failed[0]["error_code"] == "QWENPAW_TASK_CANCELLED"
    assert projector.project({"object": "response", "status": "completed"}) == []


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary', ['call', 'result'])
async def test_unclosed_preamble_rotates_before_tool_and_preserves_final_answer(boundary):
    from agentscope.event import (
        TextBlockStartEvent, TextBlockDeltaEvent, TextBlockEndEvent,
        ToolCallStartEvent, ToolResultStartEvent, ToolResultEndEvent,
    )
    from agentscope.message import ToolResultState
    from qwenpaw.runtime.envelope import Envelope

    envelope = Envelope('document-read')
    projector = CompactEventProjector('task')
    output = []
    tool_args = dict(reply_id='read', tool_call_id='read', tool_call_name='parse_documents')
    events = [TextBlockStartEvent(reply_id='read', block_id='preamble'),
              TextBlockDeltaEvent(reply_id='read', block_id='preamble', delta='我来读取文档。')]
    if boundary == 'call':
        events.append(ToolCallStartEvent(**tool_args))
    events += [ToolResultStartEvent(**tool_args),
               ToolResultEndEvent(reply_id='read', tool_call_id='read', state=ToolResultState.SUCCESS),
               TextBlockEndEvent(reply_id='read', block_id='preamble'),
               TextBlockStartEvent(reply_id='final', block_id='answer'),
               TextBlockDeltaEvent(reply_id='final', block_id='answer', delta='文档条款已识别。'),
               TextBlockEndEvent(reply_id='final', block_id='answer')]
    for event in events:
        async for native in envelope.translate_event(event):
            output.extend(projector.project(native.model_dump(mode='json')))
    async for native in envelope.finalize():
        output.extend(projector.project(native.model_dump(mode='json')))
    phases = [e for e in output if e['event'] == 'answer.phase']
    answers = [e for e in output if e['event'] == 'answer.chunk']
    assert [e['text'] for e in phases] == ['我来读取文档。']
    assert answers[-1]['text'] == '文档条款已识别。'
    assert answers[-1]['message_id'] != phases[0]['message_id']
    assert output[-1]['event'] == 'answer.completed'


@pytest.mark.parametrize('code',['DOCUMENT_PLAN_INVALID','DOCUMENT_INVENTORY_INCOMPLETE','DOCUMENT_STATISTICS_INCOMPLETE'])
def test_document_workflow_failure_stage_survives_native_event_projection(code):
    projector=CompactEventProjector('task')
    result=projector.project({'event':'error','error':{'code':code,'message':'private diagnostic'}})
    assert result[0]['event']=='answer.failed' and result[0]['error_code']==code
    assert 'private diagnostic' not in str(result)


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


async def project_native_events(events):
    from qwenpaw.runtime.envelope import Envelope
    envelope = Envelope('boundary-audit')
    projector = CompactEventProjector('task')
    output = []
    for event in events:
        async for native in envelope.translate_event(event):
            output.extend(projector.project(native.model_dump(mode='json')))
    async for native in envelope.finalize():
        output.extend(projector.project(native.model_dump(mode='json')))
    return output


@pytest.mark.asyncio
@pytest.mark.parametrize('closed', ['first', 'second', 'both'])
async def test_partial_and_repeated_text_ends_preserve_complete_answer(closed):
    from agentscope.event import TextBlockStartEvent, TextBlockDeltaEvent, TextBlockEndEvent
    events = []
    for block_id, text in [('first', '第一段'), ('second', '第二段')]:
        args = dict(reply_id='reply', block_id=block_id)
        events += [TextBlockStartEvent(**args), TextBlockDeltaEvent(**args, delta=text)]
        if closed in {block_id, 'both'}:
            events.append(TextBlockEndEvent(**args))
    if closed == 'both':
        events.append(TextBlockEndEvent(reply_id='reply', block_id='second'))
    output = await project_native_events(events)
    assert ''.join(e['text'] for e in output if e['event'] == 'answer.chunk') == '第一段第二段'
    assert output[-1]['event'] == 'answer.completed'


@pytest.mark.asyncio
@pytest.mark.parametrize('after_answer', [False, True])
async def test_late_preamble_deltas_cannot_reappear_in_answer(after_answer):
    from agentscope.event import TextBlockStartEvent, TextBlockDeltaEvent, TextBlockEndEvent, ToolResultStartEvent
    events = [TextBlockStartEvent(reply_id='read', block_id='old'),
              TextBlockDeltaEvent(reply_id='read', block_id='old', delta='读取中。'),
              ToolResultStartEvent(reply_id='read', tool_call_id='read', tool_call_name='read')]
    final = [TextBlockStartEvent(reply_id='answer', block_id='new'),
             TextBlockDeltaEvent(reply_id='answer', block_id='new', delta='实际结果。'),
             TextBlockEndEvent(reply_id='answer', block_id='new')]
    late = [TextBlockDeltaEvent(reply_id='read', block_id='old', delta='迟到的过程说明。'),
            TextBlockEndEvent(reply_id='read', block_id='old')]
    events += final + late if after_answer else late + final
    output = await project_native_events(events)
    assert [e['text'] for e in output if e['event'] == 'answer.phase'] == ['读取中。']
    assert ''.join(e['text'] for e in output if e['event'] == 'answer.chunk') == '读取中。实际结果。'


@pytest.mark.asyncio
async def test_real_cancel_after_partial_text_still_fails_when_native_cleanup_completes():
    from agentscope.event import TextBlockDeltaEvent
    from qwenpaw.runtime.envelope import Envelope
    ready = asyncio.Event()
    envelope = Envelope('cancel-boundary')

    async def source():
        async for native in envelope.translate_event(TextBlockDeltaEvent(
            reply_id='reply', block_id='text', delta='未完成的结果。',
        )):
            yield 'data: ' + native.model_dump_json() + '\n\n'
        ready.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            async for native in envelope.cancel_envelope():
                yield 'data: ' + native.model_dump_json() + '\n\n'

    async def consume():
        return [json.loads(item.removeprefix('data: '))
                async for item in project_sse_stream(source(), 'task')]

    task = asyncio.create_task(consume())
    await asyncio.wait_for(ready.wait(), timeout=1)
    task.cancel()
    output = await asyncio.wait_for(task, timeout=1)
    assert output[-1]['event'] == 'answer.failed'
    assert output[-1]['error_code'] == 'QWENPAW_TASK_CANCELLED'
    assert not any(e['event'] == 'answer.completed' for e in output)
