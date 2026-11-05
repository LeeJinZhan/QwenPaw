"""Office metadata is a routing hint, not proof of readable content."""
import hashlib
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from docx import Document
from openpyxl import Workbook
from pptx import Presentation

from bank_runtime.gateway.document_access import approved_document_call, consume_document_call
from bank_runtime.gateway.document_reads import DocumentReadLedger, result_error
from bank_runtime.sandbox.cache import PreparedSandboxFile, SandboxCacheError
from bank_runtime.sandbox.file_refs import ResolvedTaskFile
from bank_runtime.sandbox.processor import AttachmentProcessor
from bank_mineru_mcp.document_store import DocumentStore
from bank_mineru_mcp.native_office import extract_native_office
from bank_mineru_mcp.spreadsheet import extract_workbook
from bank_mineru_mcp.structured_store import StructuredStore
from bank_mineru_mcp.tools import MinerUToolService


@pytest.mark.parametrize('suffix', ['doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx'])
@pytest.mark.parametrize('mime', ['application/x-office', 'image/png', 'text/plain'])
def test_office_preparation_defers_format_decision_without_reading_body(tmp_path, suffix, mime):
    path = tmp_path / ('material.' + suffix)
    payload = b'unrecognized private document body'
    path.write_bytes(payload)
    prepared = PreparedSandboxFile(file_id='office', local_path=path, content_type=mime,
        size_bytes=len(payload), original_name=path.name, expires_at='2099-01-01T00:00:00Z')
    processor = AttachmentProcessor()
    blocks = processor.process([prepared], file_refs={'office': 'fr1_office'})
    assert len(blocks) == 1 and blocks[0].type == 'text'
    assert 'tool_required' in blocks[0].text and 'fr1_office' in blocks[0].text
    assert 'private document body' not in blocks[0].text
    assert path.read_bytes() == payload
    with pytest.raises(SandboxCacheError, match='reference'):
        processor.process([prepared])


@pytest.mark.parametrize('actual,extension', [('docx', 'doc'), ('pptx', 'ppt'), ('rtf', 'docx')])
def test_native_reader_uses_actual_office_format_with_original_bytes(tmp_path, actual, extension):
    source = tmp_path / ('original.' + extension)
    if actual == 'docx':
        doc = Document(); doc.add_paragraph('Original content 123.45'); doc.save(source)
    elif actual == 'pptx':
        deck = Presentation(); slide = deck.slides.add_slide(deck.slide_layouts[5])
        slide.shapes.title.text = 'Original content 123.45'; deck.save(source)
    else:
        source.write_bytes(br'{\rtf1\ansi Original content 123.45\par}')
    original = source.read_bytes()
    result = extract_native_office(source, tmp_path / 'result', max_bytes=8 * 1024**2)
    assert 'Original content 123.45' in result['markdown']
    assert result['source_inventory']['detected_format'] == '.' + actual
    assert source.read_bytes() == original


def test_xlsx_misnamed_xls_preserves_cells_and_formula_cache_semantics(tmp_path):
    source = tmp_path / 'original.xls'
    book = Workbook(); book.active.append(['code', 'amount'])
    book.active.append(['00123', 12.5]); book.save(source)
    original = source.read_bytes()
    result = extract_workbook(source, tmp_path / 'result', stem='document', max_bytes=8 * 1024**2)
    assert result['source_format'] == '.xlsx' and result['total_rows'] == 1
    assert '00123' in (tmp_path / 'result/sheet_00.rows.jsonl').read_text()
    assert source.read_bytes() == original


@pytest.mark.asyncio
@pytest.mark.parametrize('bad_extension', ['doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx'])
async def test_unreadable_office_does_not_discard_other_documents_or_claim_completion(tmp_path, bad_extension):
    task = tmp_path / 'task_office'; task.mkdir()
    sources = {}
    for key, extension in [('good', 'docx'), ('bad', bad_extension)]:
        path = task / (key + '.' + extension)
        if key == 'good':
            doc = Document(); doc.add_paragraph('Verified source text'); doc.save(path)
        else:
            path.write_bytes(b'unrecognized format; not a readable Office file')
        sources[key] = ResolvedTaskFile(task_id=task.name, file_id=key, path=path,
            extension='.' + extension, media_type='application/x-office', size_bytes=path.stat().st_size,
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1))
    service = MinerUToolService(file_resolver=SimpleNamespace(resolve=sources.__getitem__),
        mineru_client=object(), document_store=DocumentStore(root=tmp_path),
        structured_store=StructuredStore(root=tmp_path, process_start_key=b'k' * 32))
    args = {'documents': [{'file_id': key, 'file_ref': key} for key in sources]}
    with approved_document_call(task.name, 'parse_documents', args) as grant, consume_document_call(grant, 'parse_documents', args):
        response = await service.parse_documents(**args)
    assert response['status'] == 'partial'
    good, bad = response['items']
    assert good['status'] == 'completed' and 'Verified source text' in good['markdown']
    assert bad['status'] == 'failed' and not bad['document_ref'] and not bad['markdown']
    content = [{'type': 'text', 'text': json.dumps(response)}]
    assert result_error(content)
    ledger = DocumentReadLedger()
    ledger.observe('MinerU__parse_documents', args, content, True)
    assert not ledger.sources_complete(['good', 'bad'])
    assert ledger.sources_complete(['good'])
    assert sources['good'].media_type == 'application/x-office'


def test_original_biff_workbook_with_modern_extension_preserves_values(tmp_path):
    import xlwt
    source = tmp_path / 'original.xlsx'
    book = xlwt.Workbook(); sheet = book.add_sheet('Original')
    sheet.write(0, 0, 'code'); sheet.write(1, 0, '00123'); book.save(str(source))
    original = source.read_bytes()
    result = extract_workbook(source, tmp_path / 'result', stem='document', max_bytes=8 * 1024**2)
    assert result['source_format'] == '.xls' and result['total_rows'] == 1
    assert '00123' in (tmp_path / 'result/sheet_00.rows.jsonl').read_text()
    assert source.read_bytes() == original
