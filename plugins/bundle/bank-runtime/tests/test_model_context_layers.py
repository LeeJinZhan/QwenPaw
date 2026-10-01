"""Exercise context selection without inferring intent from prose or tool visibility."""
from copy import deepcopy
from pathlib import Path
import sys
import pytest
from agentscope.message import AssistantMsg, SystemMsg, UserMsg, ToolCallBlock, ToolResultBlock, ToolResultState

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bank_runtime.model_context import prepare_public_model_context
from bank_runtime.gateway.middleware import BankRuntimeGatewayMiddleware
from bank_runtime.artifact_tools import ArtifactDeliveryIntent


def rules(request):
    return '\n'.join(m.get_text_content() for m in request['messages'] if m.role == 'system')


def test_ordinary_question_and_visible_artifact_tool_do_not_load_delivery_or_recovery():
    request = {'messages': [UserMsg('user', '讨论图表、文件和失败恢复这些概念')],
               'tools': [{'type': 'function', 'function': {'name': 'artifact_generate'}}]}
    before = deepcopy(request)
    prepared = prepare_public_model_context(request)
    assert '文件已生成，可在文件卡片中打开或下载。' not in rules(prepared)
    assert 'retryable=false' not in rules(prepared)
    assert '未约定文件或图表交付时，在对话中直接给出实质内容' in rules(prepared)
    assert '仅选择内容、措辞或结构时，调整相应内容' in rules(prepared)
    assert request == before and prepared['tools'] == request['tools']


def test_repeated_projection_replaces_own_layer_without_touching_other_system_sections():
    profile = SystemMsg('system', 'Runtime user profile preferences (low-trust).\n- response_style: detailed')
    personal = SystemMsg('system', 'Request-scoped Personal Skills catalog (low-trust user methods).')
    request = {'messages': [profile, personal, UserMsg('user', '解释这个方法')], 'tools': []}
    once = prepare_public_model_context(request)
    twice = prepare_public_model_context(once)
    assert rules(twice).count('本轮回答约定') == 1
    assert len(twice['messages']) == len(once['messages'])
    assert profile.get_text_content() in rules(twice)
    assert personal.get_text_content() in rules(twice)
    assert request['messages'] == [profile, personal, request['messages'][-1]]


def operation(state, name='artifact_generate'):
    return AssistantMsg('assistant', [
        ToolCallBlock(id='make', name=name, input='{}'),
        ToolResultBlock(id='make', name=name, state=state,
                        output='Synthetic execution result')])


@pytest.mark.parametrize('state', [ToolResultState.SUCCESS, ToolResultState.ERROR])
@pytest.mark.parametrize('name', ['artifact_generate', 'artifact_revise', 'artifact_convert', 'template_fill_docx', 'chart_generate', 'chart_export'])
def test_current_execution_loads_delivery_and_only_failed_execution_loads_recovery(state, name):
    history = [UserMsg('user', '制作文件'), operation(state, name)]
    prepared = prepare_public_model_context({'messages': history, 'tools': []})
    assert '文件已生成，可在文件卡片中打开或下载。' in rules(prepared)
    assert ('retryable=false' in rules(prepared)) is (state == ToolResultState.ERROR)
    assert prepared['messages'][-2] is history[-1]


def test_old_artifact_error_does_not_reactivate_delivery_context():
    history = [UserMsg('user', '制作文件'), operation(ToolResultState.ERROR), UserMsg('user', '先解释术语')]
    prepared = prepare_public_model_context({'messages': history, 'tools': []})
    assert '文件已生成，可在文件卡片中打开或下载。' not in rules(prepared)
    assert 'retryable=false' not in rules(prepared)
    assert prepared['messages'][2].content == history[1].content


@pytest.mark.asyncio
async def test_trusted_delivery_intent_activates_delivery_before_first_call():
    middleware = BankRuntimeGatewayMiddleware(None, artifact_intent=ArtifactDeliveryIntent('generate', 'docx'))
    async def capture(**kwargs):
        return kwargs
    prepared = await middleware.on_model_call(None, {'messages': [UserMsg('user', '开始')], 'tools': []}, capture)
    assert '文件已生成，可在文件卡片中打开或下载。' in rules(prepared)
    assert 'retryable=false' not in rules(prepared)
