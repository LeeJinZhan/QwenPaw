from types import SimpleNamespace
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from bank_runtime.sandbox.processor import AttachmentProcessor
from bank_runtime.sandbox.cache import PreparedSandboxFile
from bank_runtime.gateway.recovery_budget import OperationRecoveryBudget


def test_mixed_attachment_failure_returns_other_files_and_records_source(tmp_path):
    files=[]
    for name,content,mime in [('good.txt',b'good source','text/plain'),('bad.pdf',b'bad pdf','application/pdf')]:
        path=tmp_path/name;path.write_bytes(content)
        files.append(PreparedSandboxFile(file_id=name,local_path=path,original_name=name,content_type=mime,
            size_bytes=len(content),expires_at='2099-01-01T00:00:00Z'))
    processor=AttachmentProcessor()
    blocks=processor.process(files,file_refs={'bad.pdf':'fr1_bad'})
    assert len(blocks)==2 and 'good source' in blocks[0].text
    assert 'bad pdf' not in blocks[1].text
    assert processor.read_failures=={'bad.pdf':'FILE_TYPE_UNSUPPORTED'}


def test_verified_recovery_releases_pending_budget_but_keeps_history():
    budget=OperationRecoveryBudget(task_limit=3)
    for i in range(6):
        value={'artifact_type':'docx','output_name':f'{i}.docx'}
        assert not budget.blocked_reason('artifact_generate',value)
        budget.fail('artifact_generate',value,'INVALID_REQUEST')
        budget.succeed('artifact_generate',value)
    assert budget.total_failures==6 and budget.pending_count==0
    assert not budget.task_exhausted


def test_attachment_failure_blocks_only_dependent_operations(monkeypatch):
    from bank_runtime.gateway.middleware import BankRuntimeGatewayMiddleware
    from bank_runtime.sandbox import tools
    from test_document_read_completion import Client
    monkeypatch.setattr(tools, 'attachment_read_state', lambda: ({'bad':'DOCUMENT_TEXT_TRUNCATED'}, {'good'}))
    middleware=BankRuntimeGatewayMiddleware(Client())
    assert middleware._source_block('artifact_generate', {'source_refs':[{'source_id':'good'}]}) == ''
    assert middleware._source_block('artifact_generate', {'source_refs':[{'source_id':'bad'}]}) == 'DOCUMENT_TEXT_TRUNCATED'
    assert middleware._source_block('artifact_generate', {}) == 'DOCUMENT_TEXT_TRUNCATED'


import pytest

@pytest.mark.asyncio
async def test_partial_text_answer_persists_gap_without_discarding_good_attachment(monkeypatch):
    from bank_runtime.gateway.middleware import BankRuntimeGatewayMiddleware
    from bank_runtime.sandbox import tools
    from bank_runtime.delivery_state import begin_delivery_state, end_delivery_state
    from agentscope.message import TextBlock
    from agentscope.model import ChatResponse
    from test_document_read_completion import Client
    monkeypatch.setattr(tools, 'attachment_read_state', lambda: ({'bad':'FILE_TYPE_UNSUPPORTED'}, {'good'}))
    middleware=BankRuntimeGatewayMiddleware(Client())
    state,token=begin_delivery_state('task_001')
    async def model(**kwargs):
        return ChatResponse(id='answer',content=[TextBlock(text='已读取材料中的金额为 7。')],is_last=True)
    try:
        response=await middleware.on_model_call(None,{},model)
        assert '仅覆盖已成功读取的部分' in response.content[0].text
        assert '金额为 7' in response.content[0].text
        assert state.analysis['delivery']=='partial'
        assert any(gap['file_id']=='bad' for gap in state.analysis['gaps'])
        middleware._check_file_completion()
    finally:
        end_delivery_state(token)


def test_deferred_inventory_cannot_count_as_a_complete_read():
    from bank_runtime.gateway.document_reads import DocumentReadLedger
    from test_document_reads_v2 import blocks
    ledger=DocumentReadLedger()
    ledger.observe('MinerU__parse_documents', {'documents':[{'file_id':'f','file_ref':'fr1_source'}]},
        blocks({'status':'completed','items':[{'file_id':'f','status':'completed', 'content_mode':'structured',
            'document_ref':'ds1_deferred','chunk_count':1,'inventory':{'sheet_count':1,'sheets':[],
                'inventory_complete':False,'next_inventory_cursor':0}}]}), True)
    assert not ledger.sources_complete(['f'])
    assert ledger.documents['ds1_deferred'].inventory_cursor==0
    assert ledger.unfinished_scopes() == [{'kind':'document','file_id':'f','impact':'scope_unread'}]
    ledger.observe('MinerU__read_range', {'document_ref':'ds1_deferred','format':'inventory','row_cursor':0},
        blocks({'document_ref':'ds1_deferred','content_mode':'inventory', 'inventory':{'sheet_count':1,
            'sheets':[{'name':'Data','rows':1,'columns':[]}],'inventory_complete':True,'next_inventory_cursor':None}}), True)
    assert ledger.documents['ds1_deferred'].inventory == {'Data':1}
    assert ledger.documents['ds1_deferred'].inventory_complete
    assert not ledger.sources_complete(['f'])
