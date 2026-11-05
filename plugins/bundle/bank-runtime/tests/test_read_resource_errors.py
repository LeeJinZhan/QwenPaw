import json
import pytest
from bank_runtime.gateway.document_reads import result_error
from bank_runtime.events import CompactEventProjector


@pytest.mark.parametrize('code', ['DOCUMENT_SOURCE_UNAVAILABLE', 'DOCUMENT_ANALYSIS_FAILED', 'DOCUMENT_RESOURCE_EXHAUSTED', 'DOCUMENT_QUEUE_TIMEOUT',
    'DOCUMENT_EXECUTION_TIMEOUT', 'DOCUMENT_ENGINE_UNAVAILABLE'])
def test_read_resource_reason_survives_ledger_and_public_projection(code):
    payload = {'status': 'failed', 'items': [{'file_id': 'file1', 'status': 'failed', 'error_code': code}]}
    assert result_error([{'type': 'text', 'text': json.dumps(payload)}]) == code
    projected = CompactEventProjector("task-read").project({"object": "response",
        "status": "failed", "error": {"code": code, "message": "private diagnostic"}})
    assert projected[-1]["error_code"] == code
    assert "private diagnostic" not in json.dumps(projected)
