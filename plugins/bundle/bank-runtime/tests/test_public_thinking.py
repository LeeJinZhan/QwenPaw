from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bank_runtime.public_thinking import PublicThinkingStream


def test_split_internal_name_is_never_published_and_answer_is_untouched():
    stream = PublicThinkingStream()
    assert stream.project({"event": "answer.thinking", "text": "准备调用 Run"}) == []
    status = {"event": "status.changed", "status": "answer.generating"}
    assert stream.project(status) == [status]
    assert stream.project({"event": "answer.thinking", "text": "time Tool Gateway。"}) == []
    assert stream.project({"event": "answer.thinking", "text": "先核对资料中的日期。"}) == [
        {"event": "answer.thinking", "text": "先核对资料中的日期。"}]
    answer = {"event": "answer.chunk", "text": "Runtime 是什么？这里是相关技术说明。"}
    assert stream.project(answer) == [answer]


def test_pending_safe_text_flushes_before_completion_and_buffer_is_bounded():
    stream = PublicThinkingStream()
    stream.project({"event": "answer.thinking", "text": "核对资料"})
    assert stream.project({"event": "answer.completed"})[0]["text"] == "核对资料"
    stream = PublicThinkingStream()
    assert stream.project({"event": "answer.thinking", "text": "x" * 20000}) == []
    assert len(stream.pending) <= 8192
    assert stream.project({"event": "answer.thinking", "text": "Runtime。"}) == []
