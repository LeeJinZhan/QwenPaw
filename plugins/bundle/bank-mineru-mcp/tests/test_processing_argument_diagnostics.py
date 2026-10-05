import io
import json
import os
from types import SimpleNamespace
from bank_mineru_mcp import processing_guest
from bank_mineru_mcp.structured_store import StructuredStoreError


def test_guest_preserves_catalog_reason_without_exception_text(monkeypatch, capsys):
    # main owns its process environment. This in-process contract test must
    # restore it, otherwise subsequent extraction tests inherit guest paths.
    for name in ('BANK_READING_WORK_ROOT','SQLITE_TMPDIR'):
        monkeypatch.setenv(name,os.environ.get(name,''))
    request = {'kind':'query','root':'/workspace/input','key':'ab'*32,'max_bytes':1024,
               'name':'aggregate','arguments':{}}
    monkeypatch.setattr(processing_guest.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(json.dumps(request).encode())))
    monkeypatch.setattr(processing_guest, 'StructuredStore', lambda **_: object())
    def failed(*_):
        raise StructuredStoreError('DOCUMENT_ARGUMENT_INVALID', 'METRIC_COLUMN')
    monkeypatch.setattr(processing_guest, 'query', failed)
    previous=os.umask(0o077)
    try:
        processing_guest.main()
    finally:
        os.umask(previous)
    value = json.loads(capsys.readouterr().out)
    assert value == {'status':'failed','error_code':'DOCUMENT_ARGUMENT_INVALID','argument_reason':'METRIC_COLUMN'}
