from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bank_runtime.public_thinking import PublicThinkingStream
import pytest


@pytest.mark.parametrize('text', ['我会先统计 token 用量。', '正在核对 local_storage 的保存行为。',
                                  '正在核对 monthly_total 字段。'])
def test_business_terms_are_not_credential_or_internal_identifier_evidence(text):
    assert PublicThinkingStream().project({'event': 'answer.thinking', 'text': text}) == [
        {'event': 'answer.thinking', 'text': text}]


@pytest.mark.parametrize('text', ['token=credential-value。', 'Authorization: Bearer abcdef。',
                                  'password: confidential。', 'api_key="confidential"。',
                                  '准备调用 artifact_generate。', '文件为 gfile_private。'])
def test_secrets_and_internal_operations_remain_filtered(text):
    assert PublicThinkingStream().project({'event': 'answer.thinking', 'text': text}) == []


def test_private_content_type_is_suppressed_even_without_sensitive_words():
    stream = PublicThinkingStream()
    assert stream.project({'event': 'answer.thinking', 'content_type': 'private_reasoning',
                           'text': '我要在这里比较几种回答方案。'}) == []


@pytest.mark.parametrize('kind', ['reasoning', 'thinking'])
def test_native_private_reasoning_never_becomes_public_progress(kind):
    from bank_runtime.events import CompactEventProjector
    projector = CompactEventProjector(runtime_task_id='task-test')
    stream = PublicThinkingStream()
    private = projector.project({'object': 'message', 'type': kind, 'status': 'in_progress',
                                 'id': 'private', 'content': '核对资料。'})
    assert private
    assert [out for event in private for out in stream.project(event)] == []
    progress = projector.project({'event': 'answer.thinking', 'text': '正在统计 token 用量。'})
    assert [out for event in progress for out in stream.project(event)] == [
        {'event': 'answer.thinking', 'text': '正在统计 token 用量。'}]
    stage = {'event': 'status.changed', 'status': 'answer.generating', 'message': '正在生成回答'}
    assert stream.project(stage) == [stage]


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
