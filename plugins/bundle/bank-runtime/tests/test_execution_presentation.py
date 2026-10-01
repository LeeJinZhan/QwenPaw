from copy import deepcopy
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bank_runtime.chart_tools import chart_export_model_result
from bank_runtime.presentation import artifact_model_result


@pytest.mark.parametrize('project', [artifact_model_result, chart_export_model_result])
@pytest.mark.parametrize('state,retryable,expected', [
    ('not_started', True, True), ('failed', True, True),
    ('execution_unknown', True, False), ('executing', True, False),
    ('failed', False, False), ('invalid-state', True, False),
])
def test_recovery_requires_confirmed_execution_and_explicit_permission(project, state, retryable, expected):
    envelope = {'status': 'failed', 'error_code': 'WORKER_UNAVAILABLE', 'details': {
        'execution_status': state, 'retryable': retryable, 'remaining_attempts': 1,
        'recovery_action': 'retry', 'private_path': '/private/secrets',
    }}
    before = deepcopy(envelope)
    result = project(envelope)
    assert result['result']['retryable'] is expected
    assert result['result']['execution_status'] == (state if state != 'invalid-state' else 'execution_unknown')
    assert result['result']['remaining_attempts'] == 1
    if not expected:
        assert result['result']['recovery_action'] != 'retry'
    assert '数值类型' not in result['presentation']['message']
    assert 'private_path' not in str(result)
    assert envelope == before


@pytest.mark.parametrize('project', [artifact_model_result, chart_export_model_result])
def test_unknown_outcome_does_not_infer_failure_or_retry_from_error_code(project):
    result = project({'status': 'failed', 'error_code': 'WORKER_TIMEOUT', 'result': {}})
    assert result['result']['execution_status'] == 'execution_unknown'
    assert result['result']['retryable'] is False
    assert result['result']['recovery_action'] == 'check_status'
    assert result['presentation']['outcome'] == 'unknown'


@pytest.mark.parametrize('project', [artifact_model_result, chart_export_model_result])
@pytest.mark.parametrize('code', ['POLICY_DENIED', 'FORBIDDEN', 'FILE_ACCESS_DENIED'])
def test_authorization_denial_cannot_be_made_retryable_by_metadata(project, code):
    result = project({'status': 'blocked', 'error_code': code, 'result': {
        'execution_status': 'not_started', 'retryable': True,
        'remaining_attempts': 1, 'recovery_action': 'retry'}})
    assert result['result']['retryable'] is False
    assert result['result']['recovery_action'] == 'stop'


@pytest.mark.parametrize('project', [artifact_model_result, chart_export_model_result])
def test_exhausted_budget_and_explicit_false_are_preserved(project):
    for details in ({'retryable': True, 'remaining_attempts': 0}, {'retryable': False}):
        result = project({'status': 'failed', 'error_code': 'ARTIFACT_RENDER_FAILED',
                          'details': {'execution_status': 'failed', **details}})
        assert result['result']['retryable'] is False


def test_presentation_keeps_actual_page_count_for_precise_delivery_check():
    result = artifact_model_result({'status': 'success', 'result': {
        'artifact_status': 'succeeded', 'generated_file_ids': ['file'], 'page_count': 7}})
    assert result['result']['page_count'] == 7
