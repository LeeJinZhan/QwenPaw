"""Real MCP schema and child-process recovery for aggregate arguments."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from bank_mineru_mcp.structured_store import StructuredStore, StructuredStoreError
from bank_mineru_mcp.spreadsheet import extract_workbook
from bank_mineru_mcp.parse_jobs import query_job
from bank_mineru_mcp.server import MinerUMcpService
from bank_mineru_mcp.config import MinerUSettings


@pytest.fixture
def workbook(tmp_path):
    (tmp_path / 'task_001').mkdir()
    source = tmp_path / 'task_001' / 'table.csv'
    source.write_text('编号,金额\n1,10\n2,20\n', encoding='utf-8')
    work = tmp_path / 'work'
    inventory = extract_workbook(source, work, stem='table')
    store = StructuredStore(root=tmp_path)
    handle = store.write(SimpleNamespace(task_id='task_001', path=source,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1)), inventory, work)
    return store, handle.document_ref


@pytest.mark.asyncio
async def test_wrong_metric_field_survives_process_and_corrected_request_succeeds(workbook):
    store, ref = workbook
    with pytest.raises(StructuredStoreError) as error:
        await query_job(store, 'aggregate', {'document_ref':ref,
            'ops':[{'metrics':[{'column':'编号','op':'count'}]}]})
    assert error.value.code == 'DOCUMENT_ARGUMENT_INVALID'
    detail = error.value.argument_error
    assert detail['reason'] == 'METRIC_FUNCTION_FIELD'
    assert detail['field'] == 'ops[].metrics[].fn'
    assert 'fn' in detail['hint'] and 'op' in detail['hint']
    result = await query_job(store, 'aggregate', {'document_ref':ref,
        'ops':[{'metrics':[{'column':'编号','fn':'count'}]}]})
    assert result['results'][0]['groups'][0]['编号:count'] == 2


@pytest.mark.asyncio
async def test_mcp_schema_teaches_exact_metric_and_filter_contract():
    service = MinerUMcpService(settings=MinerUSettings(base_url='http://mineru.test', submit_mode='file_parse', token='test'),
        tool_service=None, mineru_client=None)
    tool = next(t for t in await service.mcp.list_tools() if t.name == 'aggregate')
    op = tool.inputSchema['properties']['ops']['items']
    metric = op['properties']['metrics']['items']
    assert set(metric['required']) == {'fn','column'}
    assert 'count' in metric['properties']['fn']['enum']
    assert metric['additionalProperties'] is False
    assert op['properties']['filter']['properties']['op']['enum']


@pytest.mark.parametrize('op,reason', [
    ({'metrics':[{'column':'private /srv/secret','fn':'sum'}]}, 'METRIC_COLUMN'),
    ({'metrics':[{'column':'金额','fn':'total'}]}, 'METRIC_FUNCTION'),
    ({'metrics':[{'column':'金额','fn':'sum'}], 'group_by':'编号'}, 'GROUP_BY'),
    ({'metrics':[{'column':'金额','fn':'sum'}], 'filter':{'column':'金额','op':'in','value':1}}, 'FILTER'),
    ({'metrics':[{'column':'金额','fn':'sum'}], 'row_range':[3,1]}, 'ROW_RANGE'),
])
def test_safe_field_reasons_without_source_values(workbook, op, reason):
    store, ref = workbook
    with pytest.raises(StructuredStoreError) as error:
        store.aggregate(ref,[op])
    assert error.value.argument_error['reason'] == reason
    assert 'private' not in str(error.value.argument_error)
    assert '/srv/' not in str(error.value.argument_error)
