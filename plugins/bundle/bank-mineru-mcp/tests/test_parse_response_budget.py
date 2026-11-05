import hashlib
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from docx import Document

from bank_mineru_mcp.document_store import DocumentStore
from bank_mineru_mcp.tools import MinerUToolService, ToolContractError
from bank_runtime.gateway.document_access import approved_document_call, consume_document_call
from bank_runtime.sandbox.file_refs import ResolvedTaskFile
from bank_runtime.gateway.document_reads import DocumentReadLedger


def word_batch(root, count, characters, first_mime=None):
    task = root / 'task_batch'
    task.mkdir()
    sources = {}
    body = '文件内容' * characters
    for index in range(count):
        path = task / f'file{index}.docx'
        document = Document()
        document.add_paragraph(body)
        document.save(path)
        sources[str(index)] = ResolvedTaskFile(task_id=task.name, file_id=str(index), path=path,
            media_type=first_mime if index == 0 and first_mime else
            'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
            extension='.docx', size_bytes=path.stat().st_size,
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1))
    service = MinerUToolService(file_resolver=SimpleNamespace(resolve=sources.__getitem__),
        mineru_client=object(), document_store=DocumentStore(root=root))
    args = {'documents': [{'file_id': key, 'file_ref': key} for key in sources]}
    return service, args, body


@pytest.mark.asyncio
@pytest.mark.parametrize('count,characters', [(5, 412), (5, 415), (3, 412)])
async def test_batch_response_fits_and_every_character_remains_readable(tmp_path, count, characters):
    service, args, expected = word_batch(tmp_path, count, characters)
    with approved_document_call('task_batch', 'parse_documents', args) as grant, consume_document_call(grant, 'parse_documents', args):
        result = await service.parse_documents(**args)
        assert result['status'] == 'completed'
        assert len(json.dumps(result, ensure_ascii=False, indent=2).encode()) <= 32000
        if count == 5:
            assert any(item['content_mode'] == 'chunked' for item in result['items'])
        ledger = DocumentReadLedger()
        ledger.observe('MinerU__parse_documents', args,
            [{'type': 'text', 'text': json.dumps(result)}], True)
        for item in result['items']:
            ref = item['document_ref']
            if item['content_mode'] == 'chunked':
                assert not ledger.documents[ref].complete
            chunks = []
            cursor = None
            while True:
                page = service.read_document_chunks(item['document_ref'], cursor=cursor)
                ledger.observe('MinerU__read_document_chunks', {'document_ref': ref, 'cursor': cursor},
                    [{'type': 'text', 'text': json.dumps(page)}], True)
                chunks.extend(chunk['text'] for chunk in page['chunks'])
                if not page['has_more']:
                    break
                cursor = page['next_cursor']
            assert ''.join(chunks) == expected
            assert ledger.documents[ref].complete
            if item['content_mode'] == 'chunked':
                assert item['markdown'] is None


@pytest.mark.asyncio
@pytest.mark.parametrize('mime', ['text/plain', 'application/msword', 'application/octet-stream'])
async def test_previously_uploaded_word_mime_does_not_reject_other_files(tmp_path, mime):
    service, args, expected = word_batch(tmp_path, 3, 10, mime)
    with approved_document_call('task_batch', 'parse_documents', args) as grant, consume_document_call(grant, 'parse_documents', args):
        result = await service.parse_documents(**args)
    assert result['status'] == 'completed'
    assert all(item['markdown'] == expected for item in result['items'])


@pytest.mark.asyncio
async def test_legacy_mime_does_not_make_arbitrary_zip_a_word_file(tmp_path):
    import zipfile
    service, args, _ = word_batch(tmp_path, 3, 10, 'text/plain')
    with zipfile.ZipFile(tmp_path / 'task_batch/file0.docx', 'w') as archive:
        archive.writestr('payload.txt', 'not Word')
    with approved_document_call('task_batch', 'parse_documents', args) as grant, consume_document_call(grant, 'parse_documents', args):
        response = await service.parse_documents(**args)
    assert response['status'] == 'partial'
    assert response['items'][0]['status'] == 'failed'
    assert response['items'][0]['document_ref'] is None
    assert response['items'][0]['error_code'] in {'FILE_TYPE_UNSUPPORTED', 'DOCUMENT_PARSE_FAILED'}
    assert all(item['status'] == 'completed' for item in response['items'][1:])
