from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from pathlib import Path
from xml.etree import ElementTree as ET
import hashlib
import json
import zipfile
import pytest
from docx import Document
from openpyxl import Workbook
from bank_mineru_mcp.native_office import extract_native_office
from bank_mineru_mcp.spreadsheet import extract_workbook
from bank_mineru_mcp.tools import MinerUToolService, ToolContractError
from bank_mineru_mcp.document_store import DocumentStore
from bank_mineru_mcp.structured_store import StructuredStore
from bank_runtime.sandbox.file_refs import ResolvedTaskFile
from bank_runtime.gateway.document_access import approved_document_grant, consume_document_call


def external_relationship(path, owner, kind, rid='external1'):
    with zipfile.ZipFile(path) as archive:
        members = {info.filename: archive.read(info) for info in archive.infolist()}
    relpath = str(Path(owner).parent / '_rels' / (Path(owner).name + '.rels'))
    root = ET.fromstring(members.get(relpath, b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>'))
    ET.SubElement(root, '{http://schemas.openxmlformats.org/package/2006/relationships}Relationship',
        Id=rid, Type='http://schemas.openxmlformats.org/officeDocument/2006/relationships/'+kind,
        Target='https://external.invalid/private', TargetMode='External')
    members[relpath] = ET.tostring(root)
    with zipfile.ZipFile(path, 'w') as archive:
        for name, body in members.items(): archive.writestr(name, body)


def test_passive_word_reads_body_preserves_source_and_reports_external_gap(tmp_path, monkeypatch):
    import socket
    monkeypatch.setattr(socket, 'create_connection', lambda *a, **k: pytest.fail('No external fetch is allowed'))
    path = tmp_path/'source.docx'
    doc = Document(); doc.add_paragraph('Local body 123'); doc.save(path)
    external_relationship(path, 'word/document.xml', 'image')
    before = path.read_bytes()
    result = extract_native_office(path, tmp_path/'output', max_bytes=10*1024**2)
    assert 'Local body 123' in result['markdown']
    assert result['coverage']['external_resources'] == 'unread'
    assert 'external_resource_not_loaded' in result['diagnostics']
    assert before == path.read_bytes()


def test_external_workbook_preserves_local_values_and_missing_formula_cache(tmp_path):
    path = tmp_path/'source.xlsx'; wb = Workbook(); ws = wb.active
    ws.append(['id','value','formula']); ws.append([1,7,'=SUM(B2)']); wb.save(path)
    external_relationship(path,'xl/workbook.xml','externalLinkPath')
    before = path.read_bytes()
    result = extract_workbook(path,tmp_path/'output',stem='s',allow_partial=True)
    assert result['total_rows'] == 1
    assert 'external_resources' in result['excluded']
    assert result['sheets'][0]['formula_cache_status'] != 'complete'
    assert before == path.read_bytes()


def source_files(tmp_path, count, kind='docx'):
    task = tmp_path/'task_test'; task.mkdir()
    sources = {}
    for index in range(count):
        path = task/f'f{index}.{kind}'
        if kind == 'xlsx':
            wb=Workbook(); wb.active.append(['id','value']); wb.active.append([index, 5]); wb.save(path)
        else:
            doc=Document(); doc.add_paragraph(f'Body {index}'); doc.save(path)
        sources[f'f{index}'] = ResolvedTaskFile(task_id='task_test', file_id=f'f{index}', path=path,
            extension='.'+kind, media_type='application/octet-stream', size_bytes=path.stat().st_size,
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(), expires_at=datetime.now(timezone.utc)+timedelta(hours=1))
    return sources


@pytest.mark.asyncio
async def test_logical_batch_runs_in_groups_of_five_and_isolates_batch_failure(tmp_path):
    sources = source_files(tmp_path,12)
    class Execution:
        def __init__(self): self.calls=[]
        async def execute(self,plan):
            self.calls.append(plan)
            assert 1 <= len(plan['sources']) <= 5
            # No successful job is fabricated by this fake engine.
            return {'items':[{'file_id':s['file_id'],'status':'failed','error_code':'DOCUMENT_PARSE_FAILED'} for s in plan['sources']]}
    execution=Execution()
    service=MinerUToolService(file_resolver=SimpleNamespace(resolve=sources.__getitem__),
        mineru_client=None,document_store=DocumentStore(root=tmp_path))
    args={'documents':[{'file_id':key,'file_ref':key} for key in sources]}
    with approved_document_grant('task_test','parse_documents',args,execution=execution) as metadata:
        with consume_document_call(metadata,'parse_documents',args):
            result=await service.parse_documents(**args)
    assert [len(p['sources']) for p in execution.calls] == [5,5,2]
    assert len(result['items'])==12
    assert all(item['error_code']=='DOCUMENT_PARSE_FAILED' for item in result['items'])


@pytest.mark.asyncio
async def test_large_batch_returns_every_reference_and_deferred_inventory_is_readable(tmp_path):
    sources=source_files(tmp_path,50,'xlsx')
    service=MinerUToolService(file_resolver=SimpleNamespace(resolve=sources.__getitem__),mineru_client=None,
        document_store=DocumentStore(root=tmp_path),structured_store=StructuredStore(root=tmp_path))
    args={'documents':[{'file_id':key,'file_ref':key} for key in sources]}
    with approved_document_grant('task_test','parse_documents',args) as metadata:
        with consume_document_call(metadata,'parse_documents',args):
            result=await service.parse_documents(**args)
            assert result['status']=='completed'
            assert len(result['items'])==50
            assert len(json.dumps(result,ensure_ascii=False,indent=2).encode())<=32000
            for item in result['items']:
                assert item['inventory']['next_inventory_cursor']==0
                inventory=service.structured_store.inventory(item['document_ref'])
                assert inventory['total_rows']==1 and inventory['sheets'][0]['rows']==1


@pytest.mark.asyncio
async def test_foreign_source_in_late_batch_is_rejected_before_any_execution(tmp_path):
    sources=source_files(tmp_path,6); sources['f5']=replace(sources['f5'],task_id='foreign')
    class Execution:
        async def execute(self,plan): pytest.fail('All manifest authorization must precede execution')
    service=MinerUToolService(file_resolver=SimpleNamespace(resolve=sources.__getitem__),mineru_client=None,
        document_store=DocumentStore(root=tmp_path))
    args={'documents':[{'file_id':key,'file_ref':key} for key in sources]}
    with approved_document_grant('task_test','parse_documents',args,execution=Execution()) as metadata:
        with consume_document_call(metadata,'parse_documents',args):
            with pytest.raises(ToolContractError) as caught: await service.parse_documents(**args)
    assert caught.value.code=='FILE_ACCESS_DENIED'


@pytest.mark.parametrize('kind', ['docx','pptx'])
def test_linked_picture_keeps_native_text_without_fetching_image(tmp_path, kind, monkeypatch):
    from PIL import Image
    import socket
    monkeypatch.setattr(socket, 'create_connection', lambda *a, **k: pytest.fail('External image must not be fetched'))
    image=tmp_path/'pixel.png'; Image.new('RGB',(4,4)).save(image)
    path=tmp_path/('source.'+kind)
    if kind=='docx':
        doc=Document(); doc.add_paragraph('Retained text'); doc.add_picture(str(image)); doc.save(path)
        owner='word/document.xml'
    else:
        from pptx import Presentation
        from pptx.util import Inches
        deck=Presentation(); slide=deck.slides.add_slide(deck.slide_layouts[6])
        slide.shapes.add_textbox(Inches(1), Inches(1), Inches(3), Inches(1)).text='Retained text'
        slide.shapes.add_picture(str(image), Inches(1), Inches(2)); deck.save(path)
        owner='ppt/slides/slide1.xml'
    from lxml import etree
    with zipfile.ZipFile(path) as archive:
        members={info.filename:archive.read(info) for info in archive.infolist()}
    relpath=str(Path(owner).parent/'_rels'/(Path(owner).name+'.rels'))
    root=etree.fromstring(members[relpath]); rid=None
    for rel in root:
        if rel.get('Type','').endswith('/image'):
            rel.set('TargetMode','External'); rel.set('Target','https://external.invalid/image.png');rid=rel.get('Id')
    members[relpath]=etree.tostring(root)
    root=etree.fromstring(members[owner]); ns='{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'
    for node in root.iter():
        if node.get(ns+'embed')==rid:
            del node.attrib[ns+'embed'];node.set(ns+'link',rid)
    members[owner]=etree.tostring(root)
    with zipfile.ZipFile(path,'w') as archive:
        for name,body in members.items(): archive.writestr(name,body)
    original=path.read_bytes()
    result=extract_native_office(path,tmp_path/'out',max_bytes=10*1024**2)
    assert 'Retained text' in result['markdown']
    assert result['coverage']['external_resources']=='unread'
    assert original==path.read_bytes()


@pytest.mark.asyncio
async def test_bad_pdf_does_not_erase_valid_word_result(tmp_path):
    sources=source_files(tmp_path,1)
    bad=tmp_path/'task_test'/'bad.pdf'; bad.write_bytes(b'not PDF')
    sources['bad']=replace(sources['f0'],file_id='bad',path=bad,extension='.pdf',media_type='application/pdf')
    service=MinerUToolService(file_resolver=SimpleNamespace(resolve=sources.__getitem__),mineru_client=None,
        document_store=DocumentStore(root=tmp_path))
    args={'documents':[{'file_id':key,'file_ref':key} for key in sources]}
    with approved_document_grant('task_test','parse_documents',args) as metadata:
        with consume_document_call(metadata,'parse_documents',args): result=await service.parse_documents(**args)
    assert result['status']=='partial'
    assert result['items'][0]['status']=='completed' and 'Body 0' in result['items'][0]['markdown']
    assert result['items'][1]['error_code']=='FILE_TYPE_UNSUPPORTED'


@pytest.mark.asyncio
async def test_fifty_native_documents_fit_without_erasing_coverage(tmp_path):
    sources=source_files(tmp_path,50)
    service=MinerUToolService(file_resolver=SimpleNamespace(resolve=sources.__getitem__),mineru_client=None,
        document_store=DocumentStore(root=tmp_path))
    args={'documents':[{'file_id':key,'file_ref':key} for key in sources]}
    with approved_document_grant('task_test','parse_documents',args) as metadata:
        with consume_document_call(metadata,'parse_documents',args):
            result=await service.parse_documents(**args)
            assert result['status']=='completed' and len(result['items'])==50
            assert len(json.dumps(result,ensure_ascii=False,indent=2).encode())<=32000
            for item in result['items']:
                assert item['document_ref'] and item['coverage']['native_text']=='parsed'
                page=service.read_document_chunks(item['document_ref'])
                assert 'Body ' in page['chunks'][0]['text']
