import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bank_runtime.presentation import artifact_model_result


@pytest.mark.parametrize('fmt',['docx','pptx'])
def test_text_patch_model_reports_preservation_and_layout_limit(fmt):
    report={'kind':'text_patch','artifact_type':fmt,'changed_texts':2,'layout':'unverified','preservation':'untouched_parts_verified'}
    raw={'artifact_type':fmt,'operation':'revise','artifact_status':'succeeded','revision_mode':'patch',
         'generated_file_ids':['file-example'],'revision_report':report}
    result=artifact_model_result({'status':'success','result':raw})
    assert result['result']['revision_report']==report
    assert result['presentation']['outcome']=='completed'
    assert '局部修改' in result['presentation']['message']
    assert '尚未核验' in result['presentation']['message']
    for status in ('running','failed'):
        result=artifact_model_result({'status':'success','result':{**raw,'artifact_status':status}})
        assert '已完成指定文字' not in result['presentation']['message']
    for extra in ({'changed_texts':True},{'layout':'verified'},{'artifact_type':'xlsx'},{'private':'secret'}):
        result=artifact_model_result({'status':'success','result':{**raw,'revision_report':{**report,**extra}}})
        assert 'revision_report' not in result['result']
        assert '已完成指定文字' not in result['presentation']['message']


@pytest.mark.parametrize('state,expected',[('failed',True),('not_started',True),('execution_unknown',False)])
def test_correctable_target_does_not_become_terminal_or_override_unknown_execution(state,expected):
    result=artifact_model_result({'status':'failed','error_code':'ARTIFACT_VALIDATION_FAILED',
        'details':{'reason':'text_patch_invalid_target','retryable':True,'execution_status':state}})
    assert result['result']['retryable'] is expected
    assert result['result']['recovery_action'] != 'stop'


def test_correctable_target_not_cached_as_permanent_source_failure():
    from bank_runtime.gateway.middleware import BankRuntimeGatewayMiddleware
    from bank_runtime.spreadsheet_messages import SPREADSHEET_FAILURES
    state=SimpleNamespace()
    error=SimpleNamespace(code='ARTIFACT_VALIDATION_FAILED',validation_hint=SPREADSHEET_FAILURES['text_patch_invalid_target'][1])
    # No source-key access should happen for a recoverable input error.
    BankRuntimeGatewayMiddleware._remember_spreadsheet_failure(state,'artifact_revise',{},error)
