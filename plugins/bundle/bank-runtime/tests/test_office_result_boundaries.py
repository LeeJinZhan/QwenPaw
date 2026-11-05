import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bank_runtime.presentation import artifact_model_result


def envelope(report, **overrides):
    return {'status': 'success', 'result': {
        'artifact_status': 'succeeded', 'artifact_type': 'xlsx', 'operation': 'revise',
        'generated_file_ids': ['example-file'], 'revision_mode': 'patch',
        'revision_report': report, **overrides}}


def test_xlsx_patch_model_receives_pending_calculation_and_refresh():
    report = {'changed_cells': 2, 'appended_rows': 0, 'calculation': 'on_open', 'refresh_required': True}
    result = artifact_model_result(envelope(report))
    assert result['result'].get('revision_report') == report
    assert result['presentation']['outcome'] == 'completed'
    assert '重新计算' in result['presentation']['message']
    assert '刷新' in result['presentation']['message']


@pytest.mark.parametrize('report', [None, {},
    {'changed_cells': True, 'appended_rows': 0, 'calculation': 'on_open', 'refresh_required': True},
    {'changed_cells': 1, 'appended_rows': 0, 'calculation': 'private/path', 'refresh_required': False},
    {'changed_cells': 1000000000, 'appended_rows': 0, 'calculation': 'on_open', 'refresh_required': True},
    {'changed_cells': 1, 'appended_rows': 0, 'calculation': 'on_open', 'refresh_required': True, 'private': 'secret'},
])
def test_invalid_revision_report_is_not_forwarded_or_presented(report):
    result = artifact_model_result(envelope(report))
    assert 'revision_report' not in result['result']
    assert '重新计算' not in result['presentation']['message']
    assert 'secret' not in str(result)


def test_revision_notice_is_only_for_completed_xlsx_revision():
    report = {'changed_cells': 1, 'appended_rows': 0, 'calculation': 'on_open', 'refresh_required': True}
    for overrides in ({'artifact_type': 'docx'}, {'operation': 'generate'}, {'artifact_status': 'running'}):
        result = artifact_model_result(envelope(report, **overrides))
        assert '重新计算' not in result['presentation']['message']


@pytest.mark.parametrize('code, expected', [('TEMPLATE_NOT_AVAILABLE', '管理员'), ('TEMPLATE_FIELD_REQUIRED', '补齐')])
def test_template_preflight_and_execution_failures_offer_distinct_next_steps(code, expected):
    from bank_runtime.gateway.client import _response_error
    from bank_runtime.presentation import failure_message
    error = _response_error({'detail': {'code': code, 'message': 'private/path',
                             'details': {'validation_hint': 'private/path'}}}, 'failed', http_status=422)
    assert expected in error.validation_hint
    assert 'private/path' not in error.validation_hint
    assert expected in failure_message(code)
    result = artifact_model_result({'status': 'failed', 'error_code': code,
                                   'details': {'execution_status': 'not_started', 'retryable': False}})
    assert expected in result['presentation']['message']
    assert result['presentation']['outcome'] == 'failed'


def test_pptx_rebuild_and_ppt_conversion_disclose_object_preservation_limits():
    result = artifact_model_result({'status': 'success', 'result': {
        'artifact_status': 'succeeded', 'artifact_type': 'pptx', 'operation': 'revise',
        'generated_file_ids': ['example-deck'], 'revision_mode': 'rebuild', 'page_count': 4}})
    assert '重新生成' in result['presentation']['message']
    assert '动画' in result['presentation']['message']
    assert result['result']['page_count'] == 4
    from bank_runtime.spreadsheet_messages import converted_copy_hint
    hint = converted_copy_hint({'provenance': {'engine': 'libreoffice'}}, artifact_type='pptx')
    assert 'PPTX' in hint and '动画' in hint
