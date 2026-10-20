"""Real MCP wire -> native Driver adapter -> read ledger regression."""
import json
import socket
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from openpyxl import Workbook

from bank_mineru_mcp.config import MinerUSettings
from bank_mineru_mcp.document_store import DocumentStore
from bank_mineru_mcp.structured_store import StructuredStore
from bank_mineru_mcp.tools import MinerUToolService
from bank_mineru_mcp.server import MinerUMcpService
from bank_runtime.gateway.document_reads import DocumentReadLedger
from qwenpaw.drivers.adapters.agentscope_tool import _blocks_from_value


class NoLayoutClient:
    async def parse(self, *args, **kwargs):
        raise AssertionError("Excel must never call the MinerU layout engine")

    async def probe(self):
        return {"status": "healthy"}

    async def close(self):
        pass


@pytest.mark.asyncio
async def test_multi_sheet_header_correction_recovers_real_wire_statistics(tmp_path):
    """Title rows, duplicate amounts and hidden data survive explicit repair."""
    task = tmp_path/'task_header'; task.mkdir(); path = task/'headers.xlsx'
    book = Workbook(); book.remove(book.active)
    profiles = {'本期':3, '补充':2, '汇总':4, '审计':1}
    amounts = {'本期':['1,000.00','-2.50','-2.50'], '补充':['10.00','10.00'], '汇总':['3.25'], '审计':['7.00']}
    for name, header in profiles.items():
        sheet = book.create_sheet(name)
        for _ in range(header-1):
            sheet.append(['报表说明', None]); sheet.merge_cells(start_row=sheet.max_row,start_column=1,end_row=sheet.max_row,end_column=2)
        sheet.append(['编号','金额元'])
        for i, amount in enumerate(amounts[name]):sheet.append([str(i),amount])
        if name == '本期':sheet.row_dimensions[header+1].hidden=True
        if name == '审计':sheet.sheet_state='hidden'
    book.save(path)
    source = SimpleNamespace(task_id='task_header',file_id='file_header',path=path,extension='.xlsx',
        media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',expires_at=datetime.now(timezone.utc)+timedelta(hours=1))
    client=NoLayoutClient()
    service=MinerUToolService(file_resolver=SimpleNamespace(resolve=lambda _:source),mineru_client=client,
        document_store=DocumentStore(root=tmp_path),structured_store=StructuredStore(root=tmp_path))
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    server=MinerUMcpService(settings=MinerUSettings(base_url='http://unused.test',submit_mode='file_parse',token='test',mcp_port=port),tool_service=service,mineru_client=client)
    await server.start()
    try:
        async with streamablehttp_client(f'http://127.0.0.1:{port}/mcp') as (read,write,_):
            async with ClientSession(read,write) as session:
                await session.initialize();ledger=DocumentReadLedger()
                async def call(name,payload):
                    from bank_runtime.gateway.document_access import approved_document_call
                    ledger.start('MinerU__'+name,payload)
                    with approved_document_call('task_header',name,payload) as meta:
                        wire=await session.call_tool(name,payload,meta=meta)
                    blocks=_blocks_from_value(wire);value=json.loads(blocks[0].text)
                    ledger.observe('MinerU__'+name,payload,blocks,not wire.isError and value.get('status')!='failed')
                    return value
                original=(await call('parse_documents',{'documents':[{'file_id':'file_header','file_ref':'authorized'}]}))['items'][0]['document_ref']
                for name in list(profiles)[:3]:
                    wrong_column=await call('read_range',{'document_ref':original,'sheet':name,'columns':['不存在的列'],'format':'records','rows':[1,1]})
                    assert wrong_column['argument_error']['reason']=='COLUMNS'
                    assert 'inventory' in wrong_column['recovery_hint']
                    assert 'header_row' not in wrong_column['recovery_hint']
                    source_rows=await call('read_range',{'document_ref':original,'sheet':name,'format':'source','rows':[1,profiles[name]]})
                    assert source_rows['records'][-1]['values']==['编号','金额元']
                    error=await call('aggregate',{'document_ref':original,'ops':[{'sheet':name,'metrics':[{'column':'金额元','fn':'sum'}],'numeric_text':'thousands'}]})
                    assert error['error_code']=='DOCUMENT_ARGUMENT_INVALID'
                    assert error['argument_error']['reason']=='METRIC_COLUMN'
                    assert 'inventory' in error['recovery_hint']
                    assert 'header_row' not in error['recovery_hint']
                assert not ledger.argument_retry_exhausted
                repaired=(await call('parse_documents',{'documents':[{'file_id':'file_header','file_ref':'authorized'}],'options':{'header_row':profiles}}))['items'][0]['document_ref']
                assert repaired != original
                inventory=(await call('read_range',{'document_ref':repaired,'format':'inventory'}))['inventory']
                assert len(inventory['sheets'])==4 and inventory['total_rows']==7
                assert next(s for s in inventory['sheets'] if s['name']=='审计')['hidden'] is True
                totals=[]
                for sheet in inventory['sheets']:
                    assert sheet['header_row']==profiles[sheet['name']]
                    assert sheet['source_rows']==profiles[sheet['name']]+len(amounts[sheet['name']])
                    stats=await call('aggregate',{'document_ref':repaired,'ops':[{'sheet':sheet['name'],'metrics':[{'column':'金额元','fn':'sum'},{'column':'编号','fn':'count'}],'numeric_text':'thousands'}]})
                    group=stats['results'][0]['groups'][0]
                    assert group['编号:count']==len(amounts[sheet['name']])
                    totals.append(group['金额元:sum'])
                assert sum(totals)==1025.25
                assert not ledger.pending and not ledger.documents[repaired].complete
                from bank_runtime.gateway.middleware import BankRuntimeGatewayMiddleware
                from agentscope.message import TextBlock
                from agentscope.model import ChatResponse
                middleware=BankRuntimeGatewayMiddleware(SimpleNamespace(config=SimpleNamespace(task_id='task_header')))
                middleware.document_reads=ledger
                async def final(**kwargs):
                    return ChatResponse(id='final',content=[TextBlock(text='4个工作表，包含隐藏表及隐藏行，排除各表标题与表头，共7条，金额合计1025.25。')],is_last=True)
                response=await middleware.on_model_call(None,{},final)
                assert response.content[0].text
                middleware._check_file_completion()
    finally:
        await server.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize('same_task', [True, False])
async def test_reading5_same_name_revision_statistics_and_raw_coverage(tmp_path, same_task):
    from bank_runtime.gateway.document_access import approved_document_call, consume_document_call
    from bank_mineru_mcp.tools import ToolContractError

    sources = {}
    for revision in (0, 1):
        task_id = 'task_original' if same_task or revision == 0 else 'task_revised'
        task = tmp_path / task_id
        task.mkdir(exist_ok=True)
        path = task / f'file_{revision}.xlsx'
        workbook = Workbook(); sheet = workbook.active; sheet.title = '台账'
        sheet.append(['记录编号', '问题大类', '问题小类', '问题描述', '风险等级', '机构', '发现日期', '金额（元）'])
        for index in range(1, 13):
            risk = ['高', '中', '低'][(index - 1) % 3]
            amount = index * 100
            if revision and index == 1:
                risk, amount = '低', 1100
            sheet.append([f'T{index:03d}', '测试大类', '测试小类', f'记录{index}', risk,
                          '测试机构', '2026-01-01', amount])
        workbook.save(path)
        sources[f'authorized-{revision}'] = SimpleNamespace(
            task_id=task_id, file_id=f'file_{revision}', path=path,
            original_name='中文问题台账（12行）.xlsx', extension='.xlsx',
            media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1))
    service = MinerUToolService(file_resolver=SimpleNamespace(resolve=sources.__getitem__),
        mineru_client=NoLayoutClient(), document_store=DocumentStore(root=tmp_path),
        structured_store=StructuredStore(root=tmp_path))
    ledgers = [DocumentReadLedger(), DocumentReadLedger()]

    async def invoke(revision, name, arguments):
        source = sources[f'authorized-{revision}']
        with approved_document_call(source.task_id, name, arguments) as grant:
            with consume_document_call(grant, name, arguments):
                value = (await service.parse_documents(**arguments) if name == 'parse_documents'
                         else await service.execute_structured_query(name, arguments))
        ledger = ledgers[revision]
        ledger.start('MinerU__' + name, arguments)
        ledger.observe('MinerU__' + name, arguments, [{'type': 'text', 'text': json.dumps(value)}], True)
        return value

    refs, statistics = [], []
    for revision, expected in [(0, {'高': 4, '中': 4, '低': 4}), (1, {'高': 3, '中': 4, '低': 5})]:
        item = (await invoke(revision, 'parse_documents', {'documents': [
            {'file_id': f'file_{revision}', 'file_ref': f'authorized-{revision}'}]}))['items'][0]
        assert item['status'] == 'completed'
        assert item['inventory']['title'] == '中文问题台账（12行）.xlsx'
        ref = item['document_ref']; refs.append(ref)
        args = {'document_ref': ref, 'ops': [{'sheet': '台账', 'group_by': ['风险等级'],
            'metrics': [{'column': '记录编号', 'fn': 'count'}, {'column': '金额（元）', 'fn': 'sum'}, {'column': '金额（元）', 'fn': 'avg'}]}]}
        stats = await invoke(revision, 'aggregate', args); statistics.append((args, stats))
        groups = stats['results'][0]['groups']
        assert {g['group']['风险等级']: g['记录编号:count'] for g in groups} == expected
        assert sum(g['金额（元）:sum'] for g in groups) == (8800 if revision else 7800)
        ledger = ledgers[revision]
        assert not ledger.pending and not ledger.documents[ref].complete
        evidence = ledger.evidence_snapshot()['documents'][0]
        assert evidence['file_id'] == f'file_{revision}' and not evidence['complete']
        assert evidence['statistics'][0]['metrics'] == args['ops'][0]['metrics']
        assert evidence['statistics'][0]['group_by'] == ['风险等级']
        assert not ledger.sources_complete([f'file_{revision}'])
        page = await invoke(revision, 'read_range', {'document_ref': ref, 'sheet': '台账', 'rows': [1, 12]})
        assert page['all_columns'] is True and page['rows_returned'] == [1, 12]
        assert ledger.documents[ref].complete
        assert ledger.evidence_snapshot()['documents'][0]['complete']
        assert ledger.sources_complete([f'file_{revision}'])
    assert refs[0] != refs[1]
    # Replaying the original query after the revision must retain its original result.
    assert await invoke(0, 'aggregate', statistics[0][0]) == statistics[0][1]
    assert await invoke(1, 'aggregate', statistics[1][0]) == statistics[1][1]
    if not same_task:
        with pytest.raises(ToolContractError) as denied:
            await invoke(1, 'aggregate', statistics[0][0])
        assert denied.value.code == 'FILE_ACCESS_DENIED'


@pytest.mark.asyncio
async def test_five_sheet_excel_wire_pagination_and_coverage(tmp_path):
    task = tmp_path / "task_test"
    task.mkdir()
    path = task / "planning.xlsx"
    workbook = Workbook()
    workbook.remove(workbook.active)
    expected = {}
    for name, count in zip(("规划总览", "年度规划", "节奏矩阵", "技术底座", "事项清单"), (17, 65, 14, 14, 15)):
        sheet = workbook.create_sheet(name)
        sheet.append(["编号", "说明"])
        for index in range(count):
            sheet.append([index + 1, "技术建设内容" * 80])
        expected[name] = count
    workbook.save(path)
    source = SimpleNamespace(task_id="task_test", file_id="file_test", path=path,
        extension=".xlsx", media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1))
    client = NoLayoutClient()
    service = MinerUToolService(file_resolver=SimpleNamespace(resolve=lambda ref: source),
        mineru_client=client, document_store=DocumentStore(root=tmp_path),
        structured_store=StructuredStore(root=tmp_path))
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = MinerUMcpService(settings=MinerUSettings(base_url="http://unused.test", submit_mode="file_parse", token="test", mcp_port=port),
        tool_service=service, mineru_client=client)
    await server.start()
    try:
        async with streamablehttp_client(f"http://127.0.0.1:{port}/mcp") as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                ledger = DocumentReadLedger()
                async def call(name, payload):
                    ledger.start("MinerU__" + name, payload)
                    from bank_runtime.gateway.document_access import approved_document_call
                    with approved_document_call("task_test", name, payload) as metadata:
                        result = await session.call_tool(name, payload, meta=metadata)
                    blocks = _blocks_from_value(result)
                    assert len(blocks) == 1
                    assert len(blocks[0].text.encode()) <= 32000
                    ledger.observe("MinerU__" + name, payload, blocks, not result.isError)
                    return json.loads(blocks[0].text)
                parsed = await call("parse_documents", {"documents": [{"file_id": "file_test", "file_ref": "authorized-test-ref"}]})
                item = parsed["items"][0]
                assert item["content_mode"] == "structured"
                assert item["inventory"]["engine"] == "table-facts-3"
                ref = item["document_ref"]
                # Rich source facts may paginate the initial sheet inventory.
                # Resolve the sheet set before proving full-workbook coverage.
                if not item['inventory']['inventory_complete']:
                    invalid_cursor = await call('read_document_chunks', {'document_ref': ref,
                        'cursor': str(item['inventory']['next_inventory_cursor']), 'limit': 5})
                    assert invalid_cursor['error_code'] == 'DOCUMENT_ARGUMENT_INVALID'
                    assert invalid_cursor['argument_error']['reason'] == 'CHUNK_CURSOR'
                    assert 'next_inventory_cursor' in invalid_cursor['recovery_hint']
                    assert 'row_cursor' in invalid_cursor['recovery_hint']
                    assert not any(key.startswith('policy:') for key in ledger.failures)
                    metadata_page = await call('read_range', {'document_ref': ref, 'format': 'inventory',
                        'row_cursor': item['inventory']['next_inventory_cursor']})
                    assert metadata_page['inventory']['inventory_complete']
                invalid = await call("read_range", {"document_ref": ref, "sheet": "规划总览", "columns": ["编号", "编号"]})
                assert invalid["error_code"] == "DOCUMENT_ARGUMENT_INVALID"
                assert not ledger.documents[ref].complete
                projected = await call("read_range", {"document_ref": ref, "sheet": "规划总览", "columns": ["编号"]})
                assert projected["all_columns"] is False
                assert ledger.documents[ref].covered_rows("规划总览") == 0
                assert not ledger.sources_complete(['file_test'])
                assert ledger.evidence_snapshot()['documents'][0]['sheets'][0]['covered_ranges'] == []
                cursor = None
                pages = 0
                while True:
                    payload = {"document_ref": ref, "cursor": cursor, "limit": 10}
                    page = await call("read_document_chunks", payload)
                    assert page == await call("read_document_chunks", payload)
                    pages += 1
                    if not page["has_more"]:
                        break
                    cursor = page["next_cursor"]
                assert pages > 1
                assert not ledger.pending
                assert ledger.documents[ref].complete
                assert {name: ledger.documents[ref].covered_rows(name) for name in expected} == expected
                assert ledger.sources_complete(['file_test'])
                assert all(sheet['complete'] for sheet in ledger.evidence_snapshot()['documents'][0]['sheets'])
                # A restarted store can resume the same authorized reference.
                restarted = StructuredStore(root=tmp_path)
                assert restarted.read_range(ref, sheet="年度规划", rows=[1, 3])["rows_scanned"] == 3
    finally:
        await server.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize('historical,query_first,large', [(False,False,False), (True,False,False),
    (False,True,False), (True,True,False), (True,True,True)])
async def test_merged_multirow_workbook_short_refs_and_explicit_metrics_through_gateway(tmp_path, monkeypatch, historical, query_first, large):
    from copy import deepcopy
    from agentscope.message import ToolCallBlock, ToolResultState
    from agentscope.permission import PermissionBehavior, PermissionDecision
    from agentscope.tool import ToolResponse
    from bank_runtime.gateway.middleware import BankRuntimeGatewayMiddleware, GatewayPermissionEngine
    from bank_runtime.sandbox import file_refs
    from bank_runtime.sandbox.file_refs import FileRefRegistry
    from bank_runtime.sandbox.cache import PreparedSandboxFile
    from qwenpaw.drivers.mcp_context import current_mcp_metadata

    root=tmp_path/'task_report'; root.mkdir(); path=root/'report.xlsx'
    book=Workbook(write_only=large)
    if large:
        sheet=book.create_sheet('流水')
        sheet.append(['流水编号','支行','日期','金额','账号'])
        for i in range(1,100001):
            sheet.append([f'TX{i:06d}', '东湖支行' if i%2 else '中心支行', '2026-01-02', i/100,
                          f'008800000000{i:08d}'])
    else:
        book.remove(book.active)
        for name, values in [('本期',[10,20]),('上期',[3,4])]:
            sheet=book.create_sheet(name); sheet.append(['经营报表',None]); sheet.merge_cells('A1:B1')
            sheet.append(['部门','金额','账号'])
            for i,v in enumerate(values): sheet.append([f'部门{i}',v,f'000012345678901234{i:02d}'])
    book.save(path)
    registry=FileRefRegistry(root=tmp_path); monkeypatch.setattr(file_refs,'_REGISTRY',registry)
    prepared = PreparedSandboxFile(file_id='file_report',local_path=path,
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        size_bytes=path.stat().st_size,original_name=path.name,expires_at='',task_id='task_report')
    if not historical:
        registry.issue(prepared, expires_at=datetime.now(timezone.utc)+timedelta(minutes=10))
    parser=NoLayoutClient()
    service=MinerUToolService(file_resolver=registry,mineru_client=parser,document_store=DocumentStore(root=tmp_path),
        structured_store=StructuredStore(root=tmp_path))
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
    server=MinerUMcpService(settings=MinerUSettings(base_url='http://unused.test',submit_mode='file_parse',token='test',mcp_port=port),
        tool_service=service,mineru_client=parser)
    class Gateway:
        config=SimpleNamespace(task_id='task_report')
        admitted=None
        calls=[]
        async def preflight(self,name,args,**kw):
            self.calls.append(name); self.admitted=deepcopy(args); return {'tool_call_id':'call'}
        async def report_guard(self,*args): pass
        async def report_result(self,*args): pass
    class Guard:
        async def check_permission(self,*args): return PermissionDecision(behavior=PermissionBehavior.ALLOW,message='allow')
    gateway=Gateway(); middleware=BankRuntimeGatewayMiddleware(gateway); engine=GatewayPermissionEngine(Guard(),middleware)
    token = None
    if historical or query_first:
        from bank_runtime.sandbox import tools
        from bank_runtime.sandbox.scope import SandboxRequestScope
        from bank_runtime.sandbox.hooks import _sandbox_function_tool
        from agentscope.message import TextBlock
        scope = SandboxRequestScope('task_report', {'task_id':'task_report',
            'expires_at':(datetime.now(timezone.utc)+timedelta(minutes=10)).isoformat()}, (), ())
        record = {'file_id':'file_report','display_name':path.name,'source':'conversation','readable':True}
        scope.historical_files = {'file_report': {**record, 'header_row': 1 if large else 2}} if historical else {}
        if not historical:
            scope.current_attachment_ids = ('file_report',)
        class Broker:
            async def search(self,*args,**kwargs): return [record]
        class Cache:
            calls=0
            async def prepare_files(self,scope,ids,broker,selection_records=None):
                assert ids==['file_report'] and selection_records==[{'file_id':'file_report','source':'conversation','selection_mode':'model_metadata_selection'}]
                self.calls+=1
                return [prepared]
        class Processor:
            read_failures={}
            def process(self,files,file_refs): return [TextBlock(text=json.dumps({'file_ref':file_refs['file_report']}))]
        state = tools.SandboxToolState(scope,Broker(),Cache(),Processor())
        token = tools.set_sandbox_tool_state(state)
        registered = [_sandbox_function_tool(tools.runtime_sandbox_files_search),_sandbox_function_tool(tools.runtime_sandbox_files_select)]
        agent = SimpleNamespace(toolkit=SimpleNamespace(tool_groups=[SimpleNamespace(tools=registered)]),_engine=engine)
        middleware.native_skills = SimpleNamespace(agent=agent,recognizes=lambda name:False)
        middleware.allowed_tool_names = frozenset({'runtime_sandbox_files_search','runtime_sandbox_files_select',
            'MinerU__parse_documents','MinerU__read_range','MinerU__read_document_chunks','MinerU__aggregate'})
    await server.start()
    try:
        async with streamablehttp_client(f'http://127.0.0.1:{port}/mcp') as (read,write,_):
            async with ClientSession(read,write) as session:
                await session.initialize()
                if historical or query_first:
                    from qwenpaw.drivers.adapters.agentscope_tool import DriverCapabilityTool
                    from qwenpaw.drivers.capabilities import DriverCapability, CapabilityExposure, DriverInvocationResult
                    async def driver_parse(invocation):
                        assert invocation.payload == gateway.admitted
                        result = await session.call_tool('parse_documents', invocation.payload, meta=current_mcp_metadata())
                        return DriverInvocationResult(ok=True, value=result)
                    capability = DriverCapability('mcp:mineru:tool:parse_documents', 'mineru', 'mcp', 'tool', 'parse_documents',
                        'parse_documents', exposure=CapabilityExposure(tool_name='MinerU__parse_documents'))
                    registered.append(DriverCapabilityTool(capability, driver_parse))
                async def invoke(name,args):
                    tool='MinerU__'+name
                    assert (await engine.check_permission(SimpleNamespace(name=tool),args)).behavior==PermissionBehavior.ALLOW
                    async def handler(**kw):
                        actual=json.loads(kw['tool_call'].input) if kw else args
                        assert actual==gateway.admitted
                        result=await session.call_tool(name,actual,meta=current_mcp_metadata())
                        yield ToolResponse(id='call',state=ToolResultState.SUCCESS,content=_blocks_from_value(result))
                    output=[item async for item in middleware.on_acting(None,{'tool_call':ToolCallBlock(id='call',name=tool,input=json.dumps(args))},handler)]
                    return json.loads(output[0].content[0].text)
                if large:
                    async def verify_rows(ref):
                        for row in (2,50001,100001):
                            result = await invoke('read_range', {'document_ref': ref, 'sheet': '流水', 'rows': [row,row], 'format': 'source'})
                            record = result['records'][0]
                            assert record['source_row'] == row
                            assert record['values'] == [f'TX{row-1:06d}', '东湖支行' if (row-1)%2 else '中心支行',
                                '2026-01-02', (row-1)/100, f'008800000000{row-1:08d}']
                            assert isinstance(record['values'][4], str) and len(record['values'][4]) == 20
                            assert result['signature']
                    await verify_rows('file_report')
                    assert gateway.calls.count('MinerU__parse_documents') == 1
                    # Repeat the incident's explicit search/select, obtaining a
                    # different current capability for the same exact source.
                    await middleware._run_file_preparation_tool('runtime_sandbox_files_search', {'query': path.name})
                    await middleware._run_file_preparation_tool('runtime_sandbox_files_select', {'file_ids':['file_report']})
                    fresh = registry.reference_for_file('file_report', expected_task_id='task_report')
                    await verify_rows(fresh)
                    await invoke('read_document_chunks', {'document_ref': fresh, 'limit':1})
                    result = await invoke('parse_documents', {'documents':[{'file_id':'file_report','file_ref':fresh}], 'options':{'header_row':1}})
                    await verify_rows(result['items'][0]['document_ref'])
                    assert not middleware.document_reads.pending and not middleware.document_reads.failures
                    assert not middleware.unresolved_file_operations
                    assert not next(iter(middleware.document_reads.documents.values())).complete
                    middleware.native_skills = None
                    from agentscope.model import ChatResponse
                    from agentscope.message import TextBlock
                    async def answer(**kwargs):
                        return ChatResponse(id='answer', content=[TextBlock(text='已核验指定的三个原始行，账号保留20位前导零文本。')], is_last=True)
                    assert isinstance(await middleware.on_model_call(None, {}, answer), ChatResponse)
                    return
                if query_first:
                    # Actual failure order: stable identity first, then source
                    # capability, then explicit parse and canonical reads.
                    source = await invoke('read_range', {'document_ref': 'file_report', 'sheet': '本期', 'rows': [3, 3], 'format': 'source'})
                    assert source['records'][0]['values'][2] == '00001234567890123400'
                    file_ref = registry.reference_for_file('file_report', expected_task_id='task_report')
                    source = await invoke('read_range', {'document_ref': file_ref, 'sheet': '本期', 'rows': [4, 4], 'format': 'source'})
                    assert source['records'][0]['values'][2] == '00001234567890123401'
                    assert not middleware.document_reads.pending
                    assert not middleware.document_reads.failures
                    assert gateway.calls.count('MinerU__parse_documents') == 1
                    if historical:
                        # Stable file identity survives; implicit preparation
                        # does not inherit a historical header interpretation.
                        doc = next(iter(middleware.document_reads.documents.values()))
                        assert doc.inventory['本期'] == 3
                result=await invoke('parse_documents',{'documents':[{'file_id':'file_report'}], 'options': {'header_row': 2}})
                item=result['items'][0]; assert item['status']=='completed'
                assert item['inventory']['engine']=='table-facts-3'
                if historical:
                    assert gateway.calls[:3]==['runtime_sandbox_files_search','runtime_sandbox_files_select','MinerU__parse_documents']
                    assert state.cache.calls==1
                for meta,expected in zip(item['inventory']['sheets'],[30,7]):
                    name=meta['name']; column=meta['columns'][1]['name']
                    source=await invoke('read_range',{'document_ref':item['document_ref'],'sheet':name,'rows':[3,4],'format':'source'})
                    assert [row['source_row'] for row in source['records']]==[3,4]
                    assert [row['values'][2] for row in source['records']]==['00001234567890123400','00001234567890123401']
                    first=await invoke('read_range',{'document_ref':'file_report','sheet':name,'rows':['1','1']})
                    assert first.get('status')!='failed'
                    invalid=await invoke('aggregate',{'document_ref':'file_report','ops':[{'sheet':name,'row_range':[1,meta['rows']],
                        'metrics':[{'column':column,'op':'sum'}]}]})
                    assert invalid['error_code'] == 'DOCUMENT_ARGUMENT_INVALID'
                    assert invalid['argument_error']['reason'] == 'METRIC_FUNCTION_FIELD'
                    stats=await invoke('aggregate',{'document_ref':'file_report','ops':[{'sheet':name,'row_range':[1,meta['rows']],
                        'metrics':[{'column':column,'fn':'sum'}]}]})
                    assert stats['results'][0]['groups'][0][column+':sum']==expected
                    assert not middleware.document_reads.pending
                # Scoped statistics cannot claim all raw cells were read.
                assert not middleware.document_reads.documents[item['document_ref']].complete
                middleware.native_skills = None
                from agentscope.model import ChatResponse
                from agentscope.message import TextBlock
                async def answer(**kwargs):
                    return ChatResponse(id='answer', content=[TextBlock(text='按指定范围，账号为00001234567890123400和00001234567890123401。')], is_last=True)
                final = await middleware.on_model_call(None, {}, answer)
                assert isinstance(final, ChatResponse)
    finally:
        await server.stop()
        registry.revoke_task('task_report')
        if token is not None:
            tools.reset_sandbox_tool_state(token)


@pytest.mark.asyncio
@pytest.mark.parametrize('row_count,column_count', [(7445, 4), (600, 13)])
async def test_ledger_workbook_batch_statistics_finish_without_reading_every_row(tmp_path, monkeypatch, row_count, column_count):
    from unittest.mock import AsyncMock
    from bank_mineru_mcp import parse_jobs
    from bank_runtime.gateway.document_access import approved_document_call, consume_document_call
    root = tmp_path / 'task_statistics'; root.mkdir()
    path = root / 'file_opaque.xlsx'
    book = Workbook(); sheet = book.active; sheet.title = 'Sheet1'
    sheet.append(['问题大类', '问题小类', '问题描述', '风险等级'] + [f'补充字段{i}' for i in range(column_count-4)])
    for index in range(row_count):
        sheet.append([f'大类{index%3}', f'小类{index%7}', f'记录{index}', f'风险{index%2}'] + [index for _ in range(column_count-4)])
    book.create_sheet('Sheet2'); book.create_sheet('Sheet3'); book.save(path)
    source = SimpleNamespace(task_id='task_statistics', file_id='file_statistics', path=path,
        original_name='总行问题台账历史流水.xlsx', extension='.xlsx',
        media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1))
    service = MinerUToolService(file_resolver=SimpleNamespace(resolve=lambda ref: source),
        mineru_client=NoLayoutClient(), document_store=DocumentStore(root=tmp_path), structured_store=StructuredStore(root=tmp_path))
    ledger = DocumentReadLedger()
    spy = AsyncMock(wraps=parse_jobs.query_job); monkeypatch.setattr(parse_jobs, 'query_job', spy)
    async def invoke(name, arguments):
        with approved_document_call(source.task_id, name, arguments) as authorization:
            with consume_document_call(authorization, name, arguments):
                value = await service.parse_documents(**arguments) if name == 'parse_documents' else await service.execute_structured_query(name, arguments)
        ledger.start('MinerU__' + name, arguments)
        ledger.observe('MinerU__' + name, arguments, [{'type':'text', 'text':json.dumps(value, ensure_ascii=False)}], True)
        return value
    parsed = await invoke('parse_documents', {'documents':[{'file_id':source.file_id, 'file_ref':'authorized-file'}]})
    item = parsed['items'][0]; ref = item['document_ref']
    assert item['inventory']['title'] == source.original_name
    assert item['inventory']['total_rows'] == row_count
    metrics = [{'column':'问题描述', 'fn':'count'}]
    operations = [{'sheet':'Sheet1', 'group_by':group, 'metrics':metrics}
                  for group in [['问题大类','问题小类'], ['风险等级'], []]]
    result = await invoke('aggregate', {'document_ref':ref, 'ops':operations})
    assert len(result['results']) == 3 and result['truncated'] is False
    for operation in result['results']:
        assert sum(group['问题描述:count'] for group in operation['groups']) == row_count
    assert not ledger.pending
    evidence = ledger.evidence_snapshot()['documents'][0]
    assert len(evidence['statistics']) == 3
    assert [statistic['group_by'] for statistic in evidence['statistics']] == [operation.get('group_by', []) for operation in operations]
    assert all(statistic['metrics'] == metrics for statistic in evidence['statistics'])
    assert not evidence['complete'] and not ledger.sources_complete([source.file_id])
    assert ledger.documents[ref].covered_rows('Sheet1') == 0
    assert spy.await_count == 1  # One batch; no find/read/statistics loop.
    assert await invoke('aggregate', {'document_ref':ref, 'ops':operations}) == result
    assert spy.await_count == 1  # A retry reuses results, without another scan.
