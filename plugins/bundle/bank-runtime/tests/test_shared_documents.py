from datetime import datetime,timedelta,timezone
from dataclasses import dataclass
import json
from pathlib import Path
import sys
from unittest.mock import patch
import httpx
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from bank_runtime.sandbox.executor import RuntimeSandboxExecutor,AuthorizedReadingExecution,SandboxExecutorError
from bank_runtime.sandbox.shared_documents import publish,read


@dataclass
class Chunk:
    index: int
    text: str
    heading: str = ''
    page_start: int | None = None
    page_end: int | None = None


@dataclass
class Document:
    title: str
    markdown: str
    chunks: tuple
    page_count: int | None = None
    error_code: str | None = None


def execution(name,arguments):
    context=dict(task_id='task',shared_file_references=True,
        expires_at=(datetime.now(timezone.utc)+timedelta(minutes=2)).isoformat())
    return AuthorizedReadingExecution(RuntimeSandboxExecutor('http://runtime','service',context),'call',name,arguments)


@pytest.mark.asyncio
async def test_final_ocr_body_is_published_and_fresh_execution_reads_same_cursor():
    ref='dr2_'+'a'*32+'_'+'b'*64+'_'+'c'*64
    cursor='cur2_1_'+'d'*64
    body=Document('Office','original\nOCR-added',(Chunk(0,'original'),Chunk(1,'OCR-added')))
    calls=[]
    def handle(request):
        value=json.loads(request.content)
        calls.append(value)
        assert request.headers['Authorization']=='Bearer service'
        if request.url.path.endswith('/publish'):
            assert value['document']['markdown']=='original\nOCR-added'
            assert value['document']['chunks'][1]['text']=='OCR-added'
            assert 'error_code' not in value['document']
            return httpx.Response(200,json={'data':dict(document_ref=ref,chunk_count=2)})
        assert value['arguments']==dict(document_ref=ref,cursor=cursor,limit=1)
        return httpx.Response(200,json={'data':dict(document_ref=ref,chunks=[dict(index=1,text='OCR-added',heading='',page_start=None,page_end=None)],
            next_cursor=None,has_more=False,coverage=dict(read=2,total=2),recovery_hint='restart_null')})
    factory=httpx.AsyncClient
    with patch('httpx.AsyncClient',lambda **kw:factory(transport=httpx.MockTransport(handle),**kw)):
        assert await publish(execution('parse_documents',{}),'file-ref',body)==ref
        fresh=execution('read_document_chunks',dict(document_ref=ref,cursor=cursor,limit=1))
        page=await fresh.read_shared_document(fresh.arguments)
        assert page['chunks'][0]['text']=='OCR-added' and not page['has_more']
    assert len(calls)==2


@pytest.mark.asyncio
@pytest.mark.parametrize('failure',['denied','oversized','wrong-reference','bad-cursor'])
async def test_document_page_rejects_invalid_or_unauthorized_response(failure):
    ref='dr2_'+'a'*32+'_'+'b'*64+'_'+'c'*64
    def handle(request):
        if failure=='denied':return httpx.Response(403)
        if failure=='oversized':return httpx.Response(200,content=b'x'*65537)
        return httpx.Response(200,json={'data':dict(document_ref='other' if failure=='wrong-reference' else ref,
            chunks=[],next_cursor='../bad' if failure=='bad-cursor' else None,has_more=False,
            coverage=dict(read=0,total=0),recovery_hint='restart_null')})
    factory=httpx.AsyncClient
    with patch('httpx.AsyncClient',lambda **kw:factory(transport=httpx.MockTransport(handle),**kw)):
        with pytest.raises(SandboxExecutorError):await read(execution('read_document_chunks',dict(document_ref=ref,limit=1)))


def test_shared_body_and_cursor_tokens_are_redacted_between_tasks():
    from bank_runtime.session import _RUNTIME_REFERENCE
    ref='dr2_'+'a'*32+'_'+'b'*64+'_'+'c'*64
    cursor='cur2_10_'+'d'*64
    assert _RUNTIME_REFERENCE.sub('removed',ref+' '+cursor)=='removed removed'
