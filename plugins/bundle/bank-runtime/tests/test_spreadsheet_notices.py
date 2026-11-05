import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bank_runtime.artifact_tools import SpreadsheetOperationFailureError
from bank_runtime.events import CompactEventProjector
from bank_runtime.gateway.client import GatewayError, _response_error
from bank_runtime.gateway.middleware import BankRuntimeGatewayMiddleware
from bank_runtime.spreadsheet_messages import SPREADSHEET_FAILURES


@pytest.mark.parametrize('reason', [reason for reason in SPREADSHEET_FAILURES
    if reason not in {'text_patch_invalid_target','text_patch_ambiguous','text_patch_overlap'}])
def test_fixed_guidance_survives_gateway_wrapping_and_terminal_projection(reason):
    code, message = SPREADSHEET_FAILURES[reason]
    error = _response_error({'code': code, 'details': {'reason': reason,
        'retryable': False, 'validation_hint': 'private/path must never be copied'}}, 'failed', http_status=422)
    assert error.validation_hint == message
    assert error.recovery_stop_explicit
    wrapped = GatewayError(str(error), code=code, validation_hint=error.validation_hint)
    assert wrapped.validation_hint == message
    middleware = BankRuntimeGatewayMiddleware(None)
    name = 'artifact_convert' if 'conversion' in reason else 'artifact_revise'
    payload = {'source_type': 'session_file', 'source_id': 'file-example'}
    keys = middleware._operation_keys(name, payload)
    middleware._remember_spreadsheet_failure(name, payload, wrapped)
    assert keys <= middleware.unresolved_file_operations
    with pytest.raises(SpreadsheetOperationFailureError) as caught:
        middleware._check_file_completion()
    diagnostic = caught.value.message
    result = CompactEventProjector('task-example').project({'event': 'error',
        'error': {'code': 'ARTIFACT_VALIDATION_FAILED', 'message': diagnostic}})[0]
    assert result['message'] == 'SPREADSHEET_OPERATION|' + reason
    assert result['error_code'] == 'ARTIFACT_VALIDATION_FAILED'
    # A later successful recovery must not resurrect a prior format refusal.
    middleware.unresolved_file_operations.difference_update(keys)
    middleware._check_file_completion()


def test_unknown_guidance_is_not_public_and_cancellation_keeps_priority():
    error = _response_error({'code': 'ARTIFACT_TYPE_NOT_ALLOWED', 'details': {
        'reason': 'unknown', 'validation_hint': 'private/path'}}, 'failed')
    assert error.validation_hint == ''
    result = CompactEventProjector('task-example')._terminal_event('answer.failed',
        error_code='ARTIFACT_VALIDATION_FAILED', diagnostic='SPREADSHEET_OPERATION|private/path')
    assert 'private/path' not in result['message']
    cancelled = CompactEventProjector('task-example')._terminal_event('answer.failed', 'cancelled',
        error_code='ARTIFACT_VALIDATION_FAILED', diagnostic='SPREADSHEET_OPERATION|csv_revision_unsupported')
    assert cancelled['error_code'] == 'QWENPAW_TASK_CANCELLED'
    assert cancelled['message'] == '已停止生成'


@pytest.mark.asyncio
async def test_failed_execution_envelope_keeps_format_advice_and_terminal_reason():
    from test_conversion_coverage import Client, invoke
    client = Client()
    client.envelope = {'status': 'failed', 'error_code': 'ARTIFACT_TYPE_NOT_ALLOWED',
                       'result': {'reason': 'xls_revision_unsupported', 'retryable': False}}
    middleware = BankRuntimeGatewayMiddleware(client)
    response = await invoke(middleware, 'artifact_revise', {'source_type': 'session_file', 'source_id': 'file-example'})
    assert 'XLSX' in str(response)
    with pytest.raises(SpreadsheetOperationFailureError) as caught:
        middleware._check_file_completion()
    assert caught.value.message == 'SPREADSHEET_OPERATION|xls_revision_unsupported'


def test_successful_conversion_presentation_uses_verified_metadata_not_raw_notice():
    from bank_runtime.presentation import artifact_model_result
    from bank_runtime.conversion_reports import ConversionCoverage
    report = {'schema_version': '1.0', 'coverage': 'partial', 'editable': True,
              'warnings': [], 'objects': [], 'provenance': {'engine': 'libreoffice',
              'source_sha256': 'a' * 64, 'result_sha256': 'b' * 64,
              'recalculation': 'unverified', 'fidelity': 'unverified'}}
    result = artifact_model_result({'status': 'success', 'result': {
        'artifact_status': 'succeeded', 'artifact_type': 'xlsx', 'generated_file_ids': ['converted'],
        'conversion_report': report, 'conversion_notice': 'private/path'}})
    assert 'XLSX' in result['result']['conversion_notice']
    assert '尚未核验' in result['presentation']['message']
    assert 'private/path' not in str(result)
    assert '部分对象或资源未读取' not in result['presentation']['message']
    coverage = ConversionCoverage()
    coverage.observe(['converted'], report)
    assert '尚未核验' in coverage.notice
    # The wording correction does not relax coverage or read protections.
    coverage.observe(['read-converted'], report, requires_read=True)
    assert coverage.partial_read
    assert coverage.requires_scope
