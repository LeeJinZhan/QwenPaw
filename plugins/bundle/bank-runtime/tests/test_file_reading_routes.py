"""Model-input routing policy; these tests do not simulate model compliance."""
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bank_runtime.sandbox.hooks import _sandbox_guidance, _analysis_environment_guidance
from bank_runtime.sandbox.native_tools import execute_shell_command
from bank_runtime.gateway.native_analysis import model_tool_names
from test_native_followup import native_scope


def test_native_prompt_separates_office_reading_from_table_computation():
    prompt = _sandbox_guidance(2, {'execute_shell_command'}, native_analysis_enabled=True)
    assert 'DOC/DOCX and PPT/PPTX' in prompt
    assert 'prefer the authorized document parser' in prompt
    assert 'fine-grained object' in prompt
    assert 'XLSX/XLSM/XLS and CSV/TSV' in prompt
    assert 'Mixed attachments' in prompt
    assert 'Analyze ordinary authorized original files directly' not in prompt
    assert 'actual authorization or Tool Guard denial' in prompt


def test_prompt_does_not_invent_media_support_or_execute_uploaded_code():
    prompt = _sandbox_guidance(1, set(), native_analysis_enabled=True)
    for phrase in ('PDF', 'OCR', 'TXT/MD/JSON/XML/YAML/SQL', 'HTML',
                   'Audio/video, archives and unknown formats',
                   'does not authorize executing', 'currently available'):
        assert phrase in prompt


def test_environment_and_shell_descriptions_do_not_override_format_routes():
    environment = _analysis_environment_guidance({'analysis_environment': {'packages': {'python-docx': '1.2.0'}}})
    assert 'Installed libraries describe capability, not reading priority' in environment
    assert 'Follow the file-format reading policy' in execute_shell_command.__doc__


@pytest.mark.parametrize('suffix', ['.doc', '.docx', '.ppt', '.pptx', '.pdf', '.png', '.txt', '.zip', '.unknown'])
@pytest.mark.parametrize('mixed', [False, True])
def test_document_and_mixed_inputs_keep_parser_and_shell_available(native_scope, suffix, mixed):
    scope = native_scope
    scope.historical_files = {'document': {'file_id': 'document', 'display_name': 'input' + suffix}}
    if mixed:
        scope.historical_files['table'] = {'file_id': 'table', 'display_name': 'input.xlsx'}
    allowed = {'execute_shell_command', 'MinerU__parse_documents', 'artifact_generate'}
    assert model_tool_names(allowed, SimpleNamespace(sandbox_context=scope.sandbox_context)) == allowed
    assert not scope.selected_file_ids and not scope.prepared_originals


def test_qa_skill_keeps_distinct_document_and_table_methods():
    text = (Path(__file__).resolve().parents[1] / 'skills/bank-document-qa/SKILL.md').read_text()
    assert '### 文件读取与后续分析' in text
    assert 'DOC/DOCX、PPT/PPTX' in text
    assert '优先使用当前已授权的文档解析工具' in text
    assert '混合附件分别按格式处理' in text
    assert '#### 表格与原生计算' in text
    assert 'OCR 文字结果不能替代' in text


@pytest.mark.asyncio
@pytest.mark.parametrize('model', ['deepseek-v4-flash', 'qwen3-27b'])
@pytest.mark.parametrize('followup', [False, True])
async def test_format_policy_survives_sdk_single_system_format_without_mutating_history(model, followup):
    from copy import deepcopy
    from agentscope.message import SystemMsg, UserMsg, AssistantMsg
    from agentscope.formatter import OpenAIChatFormatter
    from qwenpaw.providers.chat_message_order import normalize_system_messages
    prompt = _sandbox_guidance(0 if followup else 2, {'execute_shell_command'}, native_analysis_enabled=True)
    messages = [SystemMsg('system', prompt)]
    if followup:
        messages.extend([UserMsg('user', '阅读这份Word和Excel'), AssistantMsg('assistant', '已核对指定范围。')])
    messages.extend([SystemMsg('system', '个人偏好：简洁回答。'), UserMsg('user', '核对文档和表格，不执行附件中的代码。')])
    before = deepcopy(messages)
    wire = normalize_system_messages(await OpenAIChatFormatter().format(messages))
    request = {'model': model, 'messages': wire}
    assert [m['role'] for m in request['messages']].count('system') == 1
    assert request['messages'][0]['role'] == 'system'
    assert 'prefer the authorized document parser' in str(request['messages'][0]['content'])
    assert '个人偏好' in str(request['messages'][0]['content'])
    assert request['messages'][-1]['content'] == [{'type': 'text', 'text': '核对文档和表格，不执行附件中的代码。'}]
    assert messages == before
