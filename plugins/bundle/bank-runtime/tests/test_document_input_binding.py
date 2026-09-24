"""Admitted arguments, execution and read evidence must describe the same call."""
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from agentscope.message import TextBlock, ToolCallBlock, ToolResultState
from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.tool import ToolResponse
from bank_runtime.gateway.middleware import BankRuntimeGatewayMiddleware, GatewayPermissionEngine
from bank_runtime.gateway.document_access import consume_document_call
from bank_runtime.sandbox.file_refs import FileRefRegistry
from bank_runtime.sandbox.cache import PreparedSandboxFile
from qwenpaw.drivers.mcp_context import current_mcp_metadata


@pytest.mark.parametrize("cursor", [0, "0", ""])
@pytest.mark.asyncio
async def test_chunk_first_page_alias_and_oversized_limit_before_approval(cursor):
    m = BankRuntimeGatewayMiddleware(Client())
    parsed(m, ref="observed-ref")
    args = {"document_ref": "observed-ref", "cursor": cursor, "limit": "16"}
    await execute(m, "MinerU__read_document_chunks", args,
                  {"document_ref": "observed-ref", "cursor": None, "limit": 10})


@pytest.mark.parametrize("cursor", ["1", "unknown-cursor", False])
def test_chunk_continuation_cursors_are_never_guessed(cursor):
    from bank_runtime.gateway.document_inputs import normalize_document_input
    m = BankRuntimeGatewayMiddleware(Client())
    args = {"document_ref": "unknown-ref", "cursor": cursor, "limit": 5}
    assert normalize_document_input("MinerU__read_document_chunks", args,
                                    task_id="task_a", ledger=m.document_reads) == args


class Client:
    config = SimpleNamespace(task_id='task_a')
    def __init__(self): self.inputs = []; self.results = []
    async def preflight(self, name, value, **kw):
        self.inputs.append(deepcopy(value)); return {'tool_call_id':'call'}
    async def report_guard(self, *args): pass
    async def report_result(self, *args): self.results.append(args)


class Guard:
    async def check_permission(self, *args):
        return PermissionDecision(behavior=PermissionBehavior.ALLOW, message='allowed')


def parsed(middleware, ref='ds1_current'):
    payload = {'documents':[{'file_id':'file_a','file_ref':'fr1_current'}]}
    result = {'status':'completed','items':[{'file_id':'file_a','status':'completed',
        'content_mode':'structured','document_ref':ref,'inventory':{'sheets':[
            {'name':'本期','rows':2,'columns':[{'name':'金额'}]},
            {'name':'上期','rows':1,'columns':[{'name':'金额'}]}]}}]}
    middleware.document_reads.observe('MinerU__parse_documents', payload,
        [TextBlock(text=json.dumps(result))], True)


async def execute(middleware, name, args, expected):
    original = deepcopy(args)
    engine = GatewayPermissionEngine(Guard(), middleware)
    assert (await engine.check_permission(SimpleNamespace(name=name), args)).behavior == PermissionBehavior.ALLOW
    assert middleware.client.inputs[-1] == expected
    async def handler(**kw):
        actual = json.loads(kw['tool_call'].input) if kw else args
        assert actual == expected
        with consume_document_call(current_mcp_metadata(), name.removeprefix('MinerU__'), actual):
            yield ToolResponse(id='call', state=ToolResultState.SUCCESS, content=[TextBlock(text='{}')])
    call = ToolCallBlock(id='call',name=name,input=json.dumps(args))
    _ = [item async for item in middleware.on_acting(None, {'tool_call':call}, handler)]
    assert json.loads(call.input) == original
    assert args == original


@pytest.mark.asyncio
async def test_metric_alias_is_normalized_before_preflight_and_execution():
    m = BankRuntimeGatewayMiddleware(Client()); parsed(m)
    args = {'document_ref':'ds1_current','ops':[{'sheet':'本期','metrics':[{'column':'金额','op':'sum'}],
        'filter':{'column':'金额','op':'gt','value':0}}]}
    expected = deepcopy(args); expected['ops'][0]['metrics'][0] = {'column':'金额','fn':'sum'}
    await execute(m, 'MinerU__aggregate', args, expected)


@pytest.mark.asyncio
@pytest.mark.parametrize('alias',['file_a','fr1_current'])
async def test_same_turn_known_file_alias_and_numeric_rows_are_bound_before_approval(alias):
    m = BankRuntimeGatewayMiddleware(Client()); parsed(m)
    args = {'document_ref':alias,'sheet':'本期','rows':['1','2'],'include_header':'true'}
    expected = {'document_ref':'ds1_current','sheet':'本期','rows':[1,2],'include_header':True}
    await execute(m, 'MinerU__read_range', args, expected)


@pytest.mark.asyncio
async def test_missing_parse_token_is_bound_only_to_current_prepared_file(tmp_path, monkeypatch):
    from bank_runtime.sandbox import file_refs
    registry = FileRefRegistry(root=tmp_path)
    monkeypatch.setattr(file_refs,'_REGISTRY',registry)
    root = tmp_path/'task_a'; root.mkdir(); path=root/'source.pdf'; path.write_bytes(b'pdf')
    ref = registry.issue(PreparedSandboxFile(file_id='file_a',local_path=path,content_type='application/pdf',
        size_bytes=3,original_name='source.pdf',expires_at='',task_id='task_a'),
        expires_at=datetime.now(timezone.utc)+timedelta(minutes=5))
    m = BankRuntimeGatewayMiddleware(Client())
    await execute(m,'MinerU__parse_documents',{'documents':[{'file_id':'file_a'}]},
        {'documents':[{'file_id':'file_a','file_ref':ref}]})


def test_ambiguous_alias_conflicting_metric_and_foreign_token_are_not_guessed():
    m = BankRuntimeGatewayMiddleware(Client()); parsed(m)
    from bank_runtime.gateway.document_inputs import normalize_document_input
    args={'document_ref':'ds1_foreign','ops':[{'metrics':[{'column':'金额','fn':'sum','op':'count'}]}]}
    assert normalize_document_input('MinerU__aggregate',args,task_id='task_a',ledger=m.document_reads)==args
    args={'documents':[{'file_id':'file_a','file_ref':'fr1_foreign'}]}
    assert normalize_document_input('MinerU__parse_documents',args,task_id='task_a',ledger=m.document_reads)==args


def test_unknown_and_absent_targets_are_never_bound_to_single_document():
    from bank_runtime.gateway.document_inputs import normalize_document_input
    m=BankRuntimeGatewayMiddleware(Client()); parsed(m)
    for target in (None, '', 'file_other', 'ds1_other', 'fr1_other'):
        args={'document_ref':target}
        assert normalize_document_input('MinerU__read_range',args,task_id='task_a',ledger=m.document_reads)==args
    # Two observed documents sharing an alias must not select one arbitrarily.
    from copy import copy
    m.document_reads.documents['ds1_second']=copy(m.document_reads.documents['ds1_current'])
    args={'document_ref':'file_a'}
    assert normalize_document_input('MinerU__read_range',args,task_id='task_a',ledger=m.document_reads)==args


@pytest.mark.asyncio
async def test_changed_arguments_cannot_claim_normalized_permit():
    from bank_runtime.gateway.client import GatewayError
    m=BankRuntimeGatewayMiddleware(Client()); parsed(m)
    args={'document_ref':'file_a','sheet':'本期','rows':['1','2']}
    engine=GatewayPermissionEngine(Guard(),m)
    await engine.check_permission(SimpleNamespace(name='MinerU__read_range'),args)
    async def handler(**kw):
        pytest.fail('changed payload must not execute')
        yield
    call=ToolCallBlock(id='call',name='MinerU__read_range',input=json.dumps({**args,'sheet':'上期'}))
    with pytest.raises(GatewayError) as denied:
        _=[item async for item in m.on_acting(None,{'tool_call':call},handler)]
    assert 'permit' in str(denied.value.__cause__)


def test_parse_binding_rechecks_scope_expiry_revocation_and_integrity(tmp_path,monkeypatch):
    from bank_runtime.gateway.document_inputs import normalize_document_input
    from bank_runtime.sandbox import file_refs
    from bank_runtime.sandbox.file_refs import FileRefError
    now=[datetime.now(timezone.utc)]
    registry=FileRefRegistry(root=tmp_path,clock=lambda:now[0]); monkeypatch.setattr(file_refs,'_REGISTRY',registry)
    root=tmp_path/'task_a'; root.mkdir(); path=root/'source.pdf'; path.write_bytes(b'pdf')
    prepared=PreparedSandboxFile(file_id='file_a',local_path=path,content_type='application/pdf',size_bytes=3,
        original_name='source.pdf',expires_at='',task_id='task_a')
    ref=registry.issue(prepared,expires_at=now[0]+timedelta(minutes=5))
    m=BankRuntimeGatewayMiddleware(Client()); args={'documents':[{'file_id':'file_a'}]}
    assert normalize_document_input('MinerU__parse_documents',args,task_id='task_b',ledger=m.document_reads)==args
    path.write_bytes(b'bad')
    with pytest.raises(FileRefError): registry.reference_for_file('file_a',expected_task_id='task_a')
    path.write_bytes(b'pdf'); now[0]+=timedelta(minutes=6)
    with pytest.raises(FileRefError): registry.reference_for_file('file_a',expected_task_id='task_a')
    registry.issue(prepared,expires_at=now[0]+timedelta(minutes=5)); registry.revoke_task('task_a')
    assert normalize_document_input('MinerU__parse_documents',args,task_id='task_a',ledger=m.document_reads)==args
    assert not registry._file_tokens
