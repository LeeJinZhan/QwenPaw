"""Independent statistics, incomplete pagination, and successful-read stalls."""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from agentscope.message import TextBlock, ToolCallBlock, ToolResultState
from agentscope.model import ChatResponse

from bank_runtime.artifact_tools import FileOperationsIncompleteError
from bank_runtime.delivery_state import begin_delivery_state, end_delivery_state
from bank_runtime.gateway.document_reads import DocumentReadLedger
from bank_runtime.gateway.middleware import BankRuntimeGatewayMiddleware
from test_document_read_completion import Client, invoke
from test_document_reads_v2 import (
    REF, aggregate_evidence, blocks, grouped_page, observe_parse, parse_item,
    range_result, repeat_raw_range,
)


def statistic(ledger, op, *, error=""):
    payload = {'document_ref': REF, 'ops': [op]}
    ledger.start('MinerU__aggregate', payload)
    result = {'status': 'failed', 'error_code': error} if error else {
        'document_ref': REF, 'truncated': False, 'results': [{
            'sources': [{'sheet': op.get('sheet', '支行01'), 'range': op.get('row_range', [1, 20]),
                         'rows_scanned': op.get('row_range', [1, 20])[1] - op.get('row_range', [1, 20])[0] + 1}],
            'metrics': op['metrics'], 'group_by': op.get('group_by', []),
            'filter': op.get('filter'), 'groups': [{'金额:sum': 3}],
        }]}
    ledger.observe('MinerU__aggregate', payload, blocks(result), not error)


async def invoke_group_page(middleware, offset):
    metrics = [{'column': '金额', 'fn': 'sum'}]
    op = {'sheet': '支行01', 'group_by': ['类别'], 'metrics': metrics, 'group_cursor': offset}
    # Exercise default normalization on continuation as real model calls do.
    if offset:
        op.update(filter=None, row_range=None)
    result = {'sources': [{'sheet': '支行01', 'range': [1, 20], 'rows_scanned': 20}],
              'metrics': metrics, 'group_by': ['类别'], 'filter': None, 'group_count': 3,
              'groups': [{'group': {'类别': str(offset)}, '金额:sum': 1}],
              'groups_complete': False, 'next_group_cursor': offset + 1 if offset < 2 else None}
    await invoke(middleware, 'MinerU__aggregate', {'document_ref': REF, 'ops': [op]},
                 {'document_ref': REF, 'results': [result], 'truncated': True})


@pytest.mark.parametrize('difference', [
    {'sheet': '支行02'}, {'metrics': [{'column': '金额', 'fn': 'avg'}]},
    {'filter': {'column': '类别', 'op': 'eq', 'value': '甲'}},
    {'group_by': ['类别']}, {'row_range': [1, 10]},
])
def test_other_operation_cannot_clear_real_failure(difference):
    ledger = DocumentReadLedger(); observe_parse(ledger)
    op = {'sheet': '支行01', 'metrics': [{'column': '金额', 'fn': 'sum'}]}
    failed = {**op, **difference}
    statistic(ledger, failed, error='DOCUMENT_FORMULA_CACHE_MISSING')
    statistic(ledger, op)
    assert ledger.pending
    assert ledger.error_code == 'DOCUMENT_FORMULA_CACHE_MISSING'
    statistic(ledger, failed)
    assert not ledger.pending


def test_failed_batch_requires_each_operation_to_recover():
    ledger = DocumentReadLedger(); observe_parse(ledger)
    ops = [{'sheet': sheet, 'metrics': [{'column': '金额', 'fn': 'sum'}]}
           for sheet in ('支行01', '支行02')]
    payload = {'document_ref': REF, 'ops': ops}
    ledger.start('MinerU__aggregate', payload)
    ledger.observe('MinerU__aggregate', payload,
                   blocks({'status': 'failed', 'error_code': 'MINERU_TIMEOUT'}), False)
    statistic(ledger, ops[0])
    assert ledger.pending
    statistic(ledger, ops[1])
    assert not ledger.pending


def test_default_options_do_not_create_a_different_recovery_operation():
    ledger = DocumentReadLedger(); observe_parse(ledger)
    op = {'sheet': '支行01', 'metrics': [{'column': '金额', 'fn': 'sum'}]}
    statistic(ledger, op, error='MINERU_TIMEOUT')
    statistic(ledger, {**op, 'group_by': [], 'filter': None, 'group_cursor': 0})
    # A malformed paginated result is still rejected; the real result must
    # include the paging envelope when cursor is explicitly submitted.
    assert ledger.pending
    statistic(ledger, {**op, 'group_by': [], 'filter': None})
    assert not ledger.pending


def test_parameter_correction_does_not_erase_an_earlier_execution_failure():
    ledger = DocumentReadLedger(); observe_parse(ledger)
    op = {'sheet': '支行02', 'metrics': [{'column': '金额', 'fn': 'avg'}]}
    statistic(ledger, op, error='DOCUMENT_FORMULA_CACHE_MISSING')
    statistic(ledger, op, error='DOCUMENT_ARGUMENT_INVALID')
    aggregate_evidence(ledger)
    assert ledger.pending
    assert ledger.error_code == 'DOCUMENT_FORMULA_CACHE_MISSING'


def test_raw_stall_does_not_license_unfinished_statistics():
    ledger = DocumentReadLedger(); observe_parse(ledger)
    aggregate_evidence(ledger); grouped_page(ledger, 0); repeat_raw_range(ledger)
    assert ledger.pending
    assert not ledger.successful_read_repeats


def test_successful_read_stall_does_not_exhaust_another_operation_page_budget():
    ledger = DocumentReadLedger(); observe_parse(ledger)
    repeat_raw_range(ledger, requested_end=5)
    grouped_page(ledger, 0)
    assert ledger.pending
    assert ledger.error_code == 'DOCUMENT_READ_INCOMPLETE'
    assert not ledger.successful_read_repeats


def test_pending_group_pages_block_natural_final_answer_and_artifact():
    middleware = BankRuntimeGatewayMiddleware(Client())
    observe_parse(middleware.document_reads)
    grouped_page(middleware.document_reads, 0)
    middleware._reply_text = ['支行01按类别统计结果如下：类别甲为1。']
    with pytest.raises(FileOperationsIncompleteError):
        middleware._check_file_completion()


@pytest.mark.asyncio
async def test_pending_pages_allow_recovery_calls_but_not_early_artifact_publication():
    middleware = BankRuntimeGatewayMiddleware(Client())
    await invoke(middleware, 'MinerU__parse_documents', {'documents': [{'file_id': 'f1'}]},
                 {'items': [parse_item()]})
    await invoke_group_page(middleware, 0)
    with pytest.raises(FileOperationsIncompleteError):
        await invoke(middleware, 'artifact_generate', {'artifact_type': 'markdown', 'title': '统计'}, {})
    assert middleware.client.executions == []
    async def continue_model(**kwargs):
        return ChatResponse(id='next', content=[ToolCallBlock(id='next', name='MinerU__aggregate',
            input=json.dumps({'document_ref': REF, 'ops': [{'sheet': '支行01', 'group_cursor': 1}]}))], is_last=True)
    # Incomplete does not mean exhausted: the next page remains a legal tool plan.
    response = await middleware.on_model_call(None, {}, continue_model)
    assert any(isinstance(block, ToolCallBlock) for block in response.content)
    await invoke_group_page(middleware, 2)
    with pytest.raises(FileOperationsIncompleteError):
        middleware._check_file_completion()
    await invoke_group_page(middleware, 1)
    middleware._reply_text = ['支行01按类别统计结果如下。']
    middleware._check_file_completion()


@pytest.mark.asyncio
async def test_successful_raw_repeats_can_finish_verified_statistics_without_partial_status():
    middleware = BankRuntimeGatewayMiddleware(Client())
    observe_parse(middleware.document_reads)
    aggregate_evidence(middleware.document_reads)
    repeat_raw_range(middleware.document_reads, requested_end=5)
    state, token = begin_delivery_state('task_001')
    try:
        async def model(**kwargs):
            assert not kwargs.get('tools'), 'Stop the redundant read loop.'
            return ChatResponse(id='done', content=[TextBlock(text='支行01金额合计为3。')], is_last=True)
        response = await middleware.on_model_call(None, {}, model)
        middleware._collect_reply_text(response)
        middleware._check_file_completion()
        assert not middleware._scoped_answer_confirmed
        assert state.analysis == {}, 'A successful repeated read is not a delivery gap.'
        async def no_second_attempt(**kwargs):
            raise AssertionError('The final attempt must not loop.')
        with pytest.raises(FileOperationsIncompleteError):
            await middleware.on_model_call(None, {}, no_second_attempt)
    finally:
        end_delivery_state(token)


@pytest.mark.asyncio
@pytest.mark.parametrize('answer', [
    '已完整读取所有工作表。', '支行01已完整读取。',
    '支行01金额合计为3，全部工作表均已读完。', '支行02金额合计为999。',
])
async def test_statistical_finish_cannot_license_raw_or_unsupported_claims(answer):
    middleware = BankRuntimeGatewayMiddleware(Client())
    observe_parse(middleware.document_reads); aggregate_evidence(middleware.document_reads)
    repeat_raw_range(middleware.document_reads, requested_end=5)
    async def model(**kwargs):
        return ChatResponse(id='bad', content=[TextBlock(text=answer)], is_last=True)
    with pytest.raises(FileOperationsIncompleteError):
        await middleware.on_model_call(None, {}, model)
    # An outer handler must not turn a rejected model response into success.
    with pytest.raises(FileOperationsIncompleteError):
        middleware._check_file_completion()


@pytest.mark.asyncio
async def test_statistical_finish_cannot_clear_real_raw_read_failure():
    middleware = BankRuntimeGatewayMiddleware(Client())
    observe_parse(middleware.document_reads); aggregate_evidence(middleware.document_reads)
    await invoke(middleware, 'MinerU__read_range', {'document_ref': REF, 'sheet': '支行02'},
                 {'status': 'failed', 'error_code': 'FILE_ACCESS_DENIED'}, ToolResultState.ERROR)
    async def model(**kwargs):
        return ChatResponse(id='bad', content=[TextBlock(text='支行01金额合计为3。')], is_last=True)
    with pytest.raises(FileOperationsIncompleteError):
        await middleware.on_model_call(None, {}, model)


@pytest.mark.asyncio
async def test_completed_range_replays_do_not_mask_attachment_preparation_failure(monkeypatch):
    middleware = BankRuntimeGatewayMiddleware(Client())
    observe_parse(middleware.document_reads); aggregate_evidence(middleware.document_reads)
    repeat_raw_range(middleware.document_reads, requested_end=5)
    monkeypatch.setattr('bank_runtime.sandbox.tools.attachment_read_error', lambda: 'DOCUMENT_TEXT_TRUNCATED')
    async def model(**kwargs):
        return ChatResponse(id='bad', content=[TextBlock(text='支行01金额合计为3。')], is_last=True)
    with pytest.raises(FileOperationsIncompleteError):
        await middleware.on_model_call(None, {}, model)


@pytest.mark.asyncio
async def test_cancelled_read_loop_exit_does_not_publish_a_completed_answer():
    import asyncio
    middleware = BankRuntimeGatewayMiddleware(Client())
    observe_parse(middleware.document_reads); aggregate_evidence(middleware.document_reads)
    repeat_raw_range(middleware.document_reads, requested_end=5)
    async def output():
        yield ChatResponse(id='cancel', content=[TextBlock(text='支行01金额合计为3。')], is_last=False)
        raise asyncio.CancelledError()
    async def model(**kwargs):
        return output()
    with pytest.raises(asyncio.CancelledError):
        await middleware.on_model_call(None, {}, model)
    assert not middleware._scoped_answer_confirmed
