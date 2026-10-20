"""Reasoning lifecycle regressions at the public SSE boundary."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bank_runtime.events import CompactEventProjector, project_sse_stream


def start(message_id, kind="message"):
    return {"object": "message", "type": kind, "id": message_id,
            "status": "in_progress", "content": []}


def chunk(message_id, text, delta=True):
    return {"object": "content", "type": "text", "msg_id": message_id,
            "text": text, "delta": delta}


def test_old_thinking_completion_does_not_reclassify_current_answer():
    projector = CompactEventProjector("task")
    for event in [start("r", "reasoning"), chunk("r", "分析。"),
                  start("a"), chunk("a", "前半段。")]:
        projector.project(event)
    assert projector.project(chunk("r", "分析。", False)) == []
    assert projector.project({"object": "message", "type": "reasoning", "id": "r",
                              "status": "completed", "content": "分析。"}) == []
    assert projector.project(chunk("a", "后半段。")) == [
        {"event": "answer.chunk", "message_id": "a", "text": "后半段。"}]
    assert projector.project({"object": "response", "status": "completed"}) == [
        {"event": "answer.completed", "status": "completed", "message": "回答完成"}]


def test_old_text_snapshot_does_not_rotate_new_answer():
    projector = CompactEventProjector("task")
    for event in [start("p"), chunk("p", "准备读取。"), start("tool", "plugin_call"),
                  start("a"), chunk("a", "正式答案。")]:
        projector.project(event)
    assert projector.project({"object": "message", "type": "message", "id": "p",
                              "status": "completed", "content": "准备读取。"}) == []
    assert projector.project({"object": "response", "status": "completed"})[0]["event"] == "answer.completed"


@pytest.mark.asyncio
async def test_retraction_survives_thinking_text_filter_and_can_target_old_phase():
    async def source():
        for event in [start("p"), chunk("p", "内部 gateway 分析。"),
                      start("a"), chunk("a", "正常答案。"),
                      {"object": "message", "type": "reasoning", "id": "p",
                       "status": "completed", "content": "内部 gateway 分析。"},
                      {"object": "response", "status": "completed"}]:
            yield "data: " + json.dumps(event) + "\n\n"
    result = [json.loads(item.removeprefix("data: "))
              async for item in project_sse_stream(source(), "task")]
    assert {"event": "answer.retracted", "message_id": "p"} in result
    assert not any(e["event"] == "answer.thinking" for e in result)
    assert not any(e["event"] == "answer.phase" and e.get("message_id") == "a" for e in result)


def test_new_reasoning_still_reclassifies_preamble_as_phase():
    projector = CompactEventProjector("task")
    projector.project(start("p"))
    projector.project(chunk("p", "查询资料。"))
    assert projector.project(start("r", "reasoning")) == [
        {"event": "answer.phase", "message_id": "p", "text": "查询资料。"}]


def test_growing_reasoning_snapshots_before_answer_keep_all_deltas():
    projector = CompactEventProjector("task")
    result = []
    for text in ["思", "思考", "思考"]:
        result.extend(projector.project({"object": "message", "type": "reasoning", "id": "r",
                                         "status": "in_progress", "content": text}))
    assert "".join(e["text"] for e in result) == "思考"


def test_unclassified_markup_cannot_become_answer_or_execution_phase():
    projector = CompactEventProjector("task")
    projector.project(start("a"))
    result = projector.project(chunk("a", "<think>内部内容</think>"))
    assert [e["event"] for e in result] == ["answer.failed"]
    assert projector.project(start("tool", "plugin_call")) == []
    assert projector.project({"object": "response", "status": "completed"}) == []


def test_code_quoted_tags_are_valid_public_text():
    projector = CompactEventProjector("task")
    projector.project(start("a"))
    text = "示例：`<think>内容</think>`。"
    assert projector.project(chunk("a", text))[0]["text"] == text


def test_code_quoted_tags_remain_literal_across_native_chunks():
    projector = CompactEventProjector("task")
    projector.project(start("a"))
    text = "```xml\n<think>内容</think>\n```"
    events = [e for char in text for e in projector.project(chunk("a", char))]
    assert all(e["event"] == "answer.chunk" for e in events)
    assert "".join(e["text"] for e in events) == text
