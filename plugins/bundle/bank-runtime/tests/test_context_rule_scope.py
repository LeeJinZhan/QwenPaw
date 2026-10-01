from pathlib import Path
import sys
from agentscope.message import UserMsg
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bank_runtime.model_context import prepare_public_model_context


def test_global_context_has_general_evidence_rules_without_business_case_injection():
    request = {'messages': [UserMsg('user', '解释递归')], 'tools': []}
    prepared = prepare_public_model_context(request)
    rules = '\n'.join(message.get_text_content() for message in prepared['messages'] if message.role == 'system')
    for case in ('历史事件', '天气实况', '月份合计', '网点跨期', '活跃用户数'):
        assert case not in rules
    assert '可信上下文' in rules
    assert prepared['tools'] == []
    assert request['messages'][0] in prepared['messages']


def test_goal_guidance_allows_user_to_change_goal_without_adopting_other_suggestions():
    from bank_runtime.model_context import GOAL_GUIDANCE
    assert '用户明确新增或改变目标时更新任务' in GOAL_GUIDANCE
    assert '仅选择内容、措辞或结构时' in GOAL_GUIDANCE
    assert '不同时采纳未选定的交付形式' in GOAL_GUIDANCE
    assert '已明确的文件、图表等成果任务直接交付' in GOAL_GUIDANCE


def test_conversion_tool_guidance_matches_requested_evidence_scope():
    from bank_runtime.artifact_tools import artifact_convert
    from bank_runtime.conversion_reports import ConversionCoverage
    assert 'requested analysis scope' in artifact_convert.__doc__
    assert 'Full-source conclusions require complete coverage' in artifact_convert.__doc__
    coverage = ConversionCoverage()
    coverage.observe(['file'], {'schema_version': '1.0', 'coverage': 'partial',
        'editable': False, 'warnings': ['object_unreadable'],
        'objects': [{'index': 1, 'kind': 'chart', 'status': 'unreadable'}]}, requires_read=True)
    assert '本次问题所需范围' in coverage.model_instruction
    assert '整份结论须核对全部相关分页' in coverage.model_instruction
    assert '不得声称完整读取、全量统计' in coverage.model_instruction


def test_historical_file_citations_preserve_user_presentation_preferences():
    from bank_runtime.sandbox.hooks import _sandbox_guidance
    text = _sandbox_guidance(0, {'runtime_sandbox_files_search', 'runtime_sandbox_files_select'})
    assert 'current presentation requests and applicable citation preferences' in text
    assert 'listing its display name' in text
    assert "end the answer with a '参考文件'" not in text


def test_docx_descriptions_do_not_force_one_incident_placeholder():
    from bank_runtime.artifact_schema import _official_content_schema
    schema = _official_content_schema()
    document = schema['properties']['document']
    assert document['required'] == ['title', 'recipients', 'blocks']
    for field in ('recipients', 'signatory', 'date'):
        description = document['properties'][field]['description']
        assert '用户指定的占位方式' in description
        assert '不要写进 blocks' in description
    assert '不得擅用当天日期' in document['properties']['date']['description']
