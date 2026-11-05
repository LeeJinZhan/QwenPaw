import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bank_runtime.office_assets import office_asset
from bank_runtime.gateway.recovery_budget import OperationRecoveryBudget
from bank_runtime.presentation import artifact_model_result


def test_recovery_uses_versioned_defaults_and_keeps_explicit_overrides():
    defaults = {'repeat_limit': 3, 'operation_limit': 5, 'task_limit': 12}
    with patch('bank_runtime.office_assets.execution_budget', return_value=defaults):
        configured = OperationRecoveryBudget()
        overridden = OperationRecoveryBudget(repeat_limit=2)
    assert (configured.repeat_limit, configured.operation_limit, configured.task_limit) == (3, 5, 12)
    assert overridden.repeat_limit == 2
    for value in (0, -1, True, '2'):
        with pytest.raises(ValueError): OperationRecoveryBudget(repeat_limit=value)


def test_rebuilt_word_is_not_presented_as_original_patch():
    result = artifact_model_result({'status': 'success', 'result': {
        'artifact_status': 'succeeded', 'artifact_type': 'docx', 'generated_file_ids': ['file-example'],
        'revision_mode': 'rebuild', 'quality_warnings': ['page_number_missing']}})
    assert result['presentation']['outcome'] == 'completed'
    assert '重新生成' in result['presentation']['message']
    assert '页码' in result['presentation']['message']
    assert result['result']['revision_mode'] == 'rebuild'


def test_legacy_result_and_unknown_warning_boundaries():
    legacy = {'status': 'success', 'result': {'artifact_status': 'succeeded', 'generated_file_ids': ['file-example']}}
    assert artifact_model_result(legacy)['presentation']['outcome'] == 'completed'
    with pytest.raises(ValueError):
        artifact_model_result({**legacy, 'result': {**legacy['result'], 'quality_warnings': ['private/path']}})
    assert office_asset('policy')['recovery'] == {'repeat_limit': 2, 'operation_limit': 4, 'task_limit': 16}
