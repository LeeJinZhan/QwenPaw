"""File analysis must not acquire a second model-owned completion contract."""
import copy
from types import SimpleNamespace

import pytest
from agentscope.message import TextBlock, UserMsg
from agentscope.model import ChatResponse

from bank_runtime.gateway.document_inputs import normalize_document_input
from bank_runtime.gateway.document_reads import DocumentReadLedger
from bank_runtime.gateway.middleware import BankRuntimeGatewayMiddleware
from bank_runtime.session import _sanitize_agent_state


@pytest.mark.asyncio
async def test_attachment_history_does_not_trigger_a_private_model_or_rewrite_the_request():
    state = {'state': {'context': [{'role': 'user', 'content': [], 'metadata': {
        'runtime_attachment_metadata': [{'file_id': 'file_a', 'display_name': 'report.xlsx'}]}}]}}
    agent = SimpleNamespace(state_dict=lambda: copy.deepcopy(state))
    calls = []
    reply = ChatResponse(id='reply', content=[TextBlock(text='已收到问题')], is_last=True)

    async def model(**kwargs):
        calls.append(kwargs)
        return reply

    messages = [UserMsg('user', '请解释这个表格')]
    gateway = BankRuntimeGatewayMiddleware(None)
    result = await gateway.on_model_call(agent, {'messages': messages}, model)
    assert result is reply
    assert len(calls) == 1
    user_messages = [message for message in calls[0]['messages'] if message.role == 'user']
    assert len(user_messages) == 1
    assert user_messages[0].content == messages[0].content
    assert not any('current_document_requirement' in str(message) for message in calls[0]['messages'])


def test_history_sanitization_preserves_prose_summaries_and_tool_pairing():
    token = 'ds1_' + 'a' * 64 + '_' + 'b' * 64
    state = {'state': {'summary': '用户需要解释统计口径', 'context': [
        {'role': 'user', 'content': [{'type': 'text', 'text': '读取附件'}]},
        {'role': 'assistant', 'content': [{'type': 'tool_call', 'id': 'read',
            'name': 'MinerU__read_range', 'input': '{"document_ref":"' + token + '"}'}]},
        {'role': 'tool', 'content': [{'type': 'tool_result', 'id': 'read',
            'name': 'MinerU__read_range', 'state': 'success', 'output': [
                {'type': 'text', 'text': '{"document_ref":"' + token + '","rows":12}'}]}]},
        {'role': 'assistant', 'content': [{'type': 'text', 'text': '此前解释供本轮参考'}]}]},
        'scroll': {'continuation_summary': {'active_task': '解释统计口径'}}}
    original = copy.deepcopy(state)
    sanitized = _sanitize_agent_state(state)
    assert sanitized['state']['summary'] == original['state']['summary']
    assert sanitized['scroll'] == original['scroll']
    assert sanitized['state']['context'][-1]['content'][0]['text'] == '此前解释供本轮参考'
    assert str(sanitized).count("'id': 'read'") == 2
    assert token not in str(sanitized)
    assert state == original


@pytest.mark.parametrize('arguments', [
    {'document_ref': 'ref', 'numeric_text': 'thousands', 'ops': [
        {'sheet': 's', 'metrics': [{'column': 'amount', 'fn': 'sum'}]}]},
    {'document_ref': 'ref', 'ops': [
        {'sheet': 's', 'metrics': [{'column': 'amount', 'op': 'sum'}]}]},
    {'document_ref': 'ref', 'sheet': 's', 'format': 'source', 'columns': ['D']},
])
def test_business_arguments_are_not_automatically_reinterpreted(arguments):
    original = copy.deepcopy(arguments)
    assert normalize_document_input('MinerU__aggregate' if 'ops' in arguments else 'MinerU__read_range',
        arguments, task_id='task_a', ledger=DocumentReadLedger()) == original
    assert arguments == original
