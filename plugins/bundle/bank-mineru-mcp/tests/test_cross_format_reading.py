import hashlib
import io
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from PIL import Image
from docx import Document
from openpyxl import Workbook
from pptx import Presentation
from pypdf import PdfWriter
import xlwt

from bank_runtime.sandbox.cache import PreparedSandboxFile, SandboxCacheError
from bank_runtime.sandbox.processor import AttachmentProcessor
from bank_runtime.sandbox.file_refs import ResolvedTaskFile
from bank_runtime.gateway.document_access import approved_document_call, consume_document_call
from bank_runtime.gateway.document_reads import DocumentReadLedger
from bank_mineru_mcp.tools import MinerUToolService, _validate_source
from bank_mineru_mcp.document_store import DocumentStore
from bank_mineru_mcp.structured_store import StructuredStore

CASES = [
    ('pdf', 'text/plain', 'application/pdf'),
    ('png', 'image/x-png', 'image/png'), ('png', 'image/jpeg', 'image/png'),
    ('jpg', 'image/jpg', 'image/jpeg'),
    ('csv', 'application/vnd.ms-excel', 'text/csv'),
    ('csv', '', 'text/csv'), ('csv', 'application/octet-stream', 'text/csv'),
    ('xls', 'text/plain', 'application/vnd.ms-excel'),
    ('xlsx', 'text/plain', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'),
    ('pptx', 'application/vnd.ms-powerpoint', 'application/vnd.openxmlformats-officedocument.presentationml.presentation'),
    ('docx', 'application/msword', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
    ('doc', 'application/rtf', 'application/msword'),
]


def fixture(path):
    suffix = path.suffix
    if suffix == '.pdf':
        writer = PdfWriter(); writer.add_blank_page(width=72, height=72); writer.write(path)
    elif suffix in {'.png', '.jpg'}:
        Image.new('RGB', (10, 10), 'white').save(path)
    elif suffix == '.csv':
        path.write_text('name,amount\none,100\n')
    elif suffix == '.xls':
        book = xlwt.Workbook(); sheet = book.add_sheet('data'); sheet.write(0,0,'name'); sheet.write(1,0,'one'); book.save(str(path))
    elif suffix == '.xlsx':
        book = Workbook(); book.active.append(['name']); book.active.append(['one']); book.save(path)
    elif suffix == '.pptx':
        deck = Presentation(); slide = deck.slides.add_slide(deck.slide_layouts[1]); slide.shapes.title.text = 'Title'; deck.save(path)
    elif suffix == '.docx':
        doc = Document(); doc.add_paragraph('Word body'); doc.save(path)
    else:
        path.write_bytes(br'{\rtf1\ansi body 123\par}')


def source(path, mime, key='source'):
    return ResolvedTaskFile(task_id=path.parent.name,file_id=key,path=path,extension=path.suffix,
        media_type=mime,original_name=path.name,size_bytes=path.stat().st_size,
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),expires_at=datetime.now(timezone.utc)+timedelta(hours=1))


def prepared(src):
    return PreparedSandboxFile(file_id=src.file_id,local_path=src.path,content_type=src.media_type,
        original_name=src.path.name,size_bytes=src.size_bytes,expires_at=src.expires_at.isoformat(),task_id=src.task_id)


@pytest.mark.parametrize('ext,mime,canonical', CASES)
@pytest.mark.asyncio
async def test_historical_mime_passes_preparation_and_tool_without_changing_source(tmp_path, ext, mime, canonical):
    task = tmp_path/'task_format'; task.mkdir(); path=task/('file.'+ext); fixture(path)
    src=source(path,mime); original=path.read_bytes()
    processor=AttachmentProcessor()
    blocks=processor.process([prepared(src)],file_refs={'source':'ref'})
    if ext in {'png','jpg'}:
        assert blocks[0].source.media_type == canonical
    else:
        assert 'tool_required' in blocks[0].text
    _validate_source(src)
    class Remote:
        async def parse(self, sources, **kwargs):
            assert sources[0].media_type == canonical
            return {'results':{'remote':{'md_content':'Read body'}}},{'source':'remote'}
    service=MinerUToolService(file_resolver=SimpleNamespace(resolve=lambda ref:src),mineru_client=Remote(),
        document_store=DocumentStore(root=tmp_path),structured_store=StructuredStore(root=tmp_path))
    args={'documents':[{'file_id':'source','file_ref':'ref'}]}
    with approved_document_call(task.name,'parse_documents',args) as g,consume_document_call(g,'parse_documents',args):
        result=await service.parse_documents(**args)
        assert result['status']=='completed'
        assert result['items'][0]['media_type']==canonical
    assert path.read_bytes()==original
    assert src.media_type==mime


@pytest.mark.parametrize('extension,body,mime', [
    ('pdf',b'not pdf','text/plain'), ('png',b'\x89PNG\r\n\x1a\n','image/jpeg'),
    ('csv',b'a\x00b','application/vnd.ms-excel'),
])
def test_mime_repair_never_accepts_invalid_bytes(tmp_path,extension,body,mime):
    path=tmp_path/('bad.'+extension);path.write_bytes(body)
    processor = AttachmentProcessor()
    blocks = processor.process([prepared(source(path,mime))],file_refs={'source':'ref'})
    assert len(blocks) == 1 and '正文未读取' in blocks[0].text
    assert processor.read_failures == {'source':'FILE_TYPE_UNSUPPORTED'}
    assert not processor.read_successes


@pytest.mark.asyncio
async def test_mixed_word_and_rtf_prepare_before_parsing(tmp_path):
    paths=[tmp_path/'one.docx',tmp_path/'two.doc']
    for path in paths:fixture(path)
    files=[source(paths[0],CASES[-2][2],'one'),source(paths[1],'application/msword','two')]
    blocks=AttachmentProcessor().process([prepared(s) for s in files],file_refs={'one':'one','two':'two'})
    assert len(blocks)==2
    assert all('tool_required' in b.text for b in blocks)


@pytest.mark.asyncio
async def test_real_ppt_long_table_remains_fully_readable_and_requires_all_pages(tmp_path):
    task=tmp_path/'task_table';task.mkdir();path=task/'table.pptx'
    body='中文长单元格'*2200
    deck=Presentation();slide=deck.slides.add_slide(deck.slide_layouts[5]);table=slide.shapes.add_table(2,2,0,0,4000000,1000000).table
    table.cell(0,0).text='序号';table.cell(0,1).text='内容';table.cell(1,0).text='1';table.cell(1,1).text=body;deck.save(path)
    src=source(path,'application/vnd.openxmlformats-officedocument.presentationml.presentation')
    service=MinerUToolService(file_resolver=SimpleNamespace(resolve=lambda ref:src),mineru_client=object(),document_store=DocumentStore(root=tmp_path))
    args={'documents':[{'file_id':'source','file_ref':'ref'}]}
    with approved_document_call(task.name,'parse_documents',args) as g,consume_document_call(g,'parse_documents',args):
        result=await service.parse_documents(**args);ref=result['items'][0]['document_ref']
        ledger=DocumentReadLedger();ledger.observe('MinerU__parse_documents',args,[{'type':'text','text':json.dumps(result)}],True)
        text='';cursor=None;pages=0
        while True:
            assert not ledger.documents[ref].complete
            page=service.read_document_chunks(ref,cursor=cursor,limit=1)
            assert len(json.dumps(page,ensure_ascii=False,indent=2).encode())<=32000
            ledger.observe('MinerU__read_document_chunks',{'document_ref':ref,'cursor':cursor},[{'type':'text','text':json.dumps(page)}],True)
            text+=''.join(c['text'] for c in page['chunks']);pages+=1
            cursor=page['next_cursor']
            if not page['has_more']:break
        assert body in text and '序号' in text and '内容' in text
        assert pages>1 and ledger.documents[ref].complete
