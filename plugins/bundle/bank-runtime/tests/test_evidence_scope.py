"""Evidence boundaries are independent of prose, language and other work."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest
from agentscope.message import TextBlock, ToolCallBlock
from agentscope.model import ChatResponse
from bank_runtime.gateway.document_reads import DocumentReadLedger, _Read
from bank_runtime.gateway.middleware import BankRuntimeGatewayMiddleware
from bank_runtime.artifact_tools import DocumentReadIncompleteError


@pytest.mark.parametrize('text', [
    '我没有读取全文，只查看了前两行。', '当前不能提供总计。',
    '这里的最大问题是缺少日期列。', '引用：“全文”是一个字段名。',
    'I have only checked two rows.', 'Je ne connais pas le total.',
])
def test_body_words_do_not_change_structured_completion(text):
    middleware = BankRuntimeGatewayMiddleware(SimpleNamespace())
    middleware.document_reads.documents['r'] = _Read('f', 0,
        inventory={'Sheet': 10}, covered={'Sheet': [[1, 2]]}, touched={'Sheet'})
    middleware._reply_text = [text]
    middleware._check_file_completion()
    snapshot = middleware.document_reads.evidence_snapshot()
    assert snapshot['documents'][0]['complete'] is False
    assert snapshot['documents'][0]['sheets'][0]['covered_ranges'] == [[1, 2]]


def test_snapshot_separates_raw_read_and_statistics_without_language_inference():
    ledger = DocumentReadLedger()
    evidence = {'sources': [{'sheet': 'S', 'range': [1, 20], 'rows_scanned': 20}],
        'metrics': [{'column': 'Revenue', 'fn': 'sum'}],
        'filter': {'column': 'Region', 'op': 'eq', 'value': 'North'},
        'group_by': ['Month'], 'groups': [{'Month': 'Jan', 'Revenue:sum': 10}]}
    ledger.documents['r'] = _Read('f', 0, inventory={'S': 20}, aggregates=[evidence])
    doc = ledger.evidence_snapshot()['documents'][0]
    assert doc['complete'] is False
    assert doc['statistics'][0]['sources'] == evidence['sources']
    assert doc['statistics'][0]['metrics'] == evidence['metrics']
    assert doc['statistics'][0]['filter'] == evidence['filter']
    assert doc['statistics'][0]['group_by'] == ['Month']
    assert 'groups' not in doc['statistics'][0]  # Context describes evidence, not another copy of data.


@pytest.mark.asyncio
@pytest.mark.parametrize('source,blocked', [('good', False), ('bad', True), (None, True)])
async def test_conversion_gap_applies_to_referenced_sources(source, blocked):
    middleware = BankRuntimeGatewayMiddleware(SimpleNamespace())
    middleware.conversion_coverage.observe(['bad'], {
        'schema_version': '1.0', 'coverage': 'partial', 'editable': False,
        'warnings': ['object_unreadable'],
        'objects': [{'index': 1, 'kind': 'object', 'status': 'unreadable'}]}, requires_read=True)
    payload = {'artifact_type': 'docx', 'content': {'paragraphs': ['Independent result']}}
    if source:
        payload['source_refs'] = [{'source_type': 'session_file', 'source_id': source}]
    async def model(**kwargs):
        return ChatResponse(id='r', content=[ToolCallBlock(id='t', name='artifact_generate',
            input=json.dumps(payload))], is_last=True)
    if blocked:
        with pytest.raises(DocumentReadIncompleteError):
            await middleware.on_model_call(None, {}, model)
    else:
        result = await middleware.on_model_call(None, {}, model)
        assert result.content[0].name == 'artifact_generate'


@pytest.mark.asyncio
async def test_completed_range_repeat_does_not_remove_independent_tools():
    middleware = BankRuntimeGatewayMiddleware(SimpleNamespace())
    middleware.document_reads.documents['r'] = _Read('f', 0,
        inventory={'S': 10}, covered={'S': [[1, 2]]}, no_progress=3, last_range_complete=True)
    schema = {'type': 'function', 'function': {'name': 'chart_export'}}
    async def model(**kwargs):
        assert schema in kwargs['tools']
        return ChatResponse(id='r', content=[ToolCallBlock(id='t', name='chart_export',
            input='{"chart_id":"independent","format":"png"}')], is_last=True)
    result = await middleware.on_model_call(None, {'tools': [schema]}, model)
    assert result.content[0].name == 'chart_export'
