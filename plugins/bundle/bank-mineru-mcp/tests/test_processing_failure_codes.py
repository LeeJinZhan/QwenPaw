import errno
import os
import io
import json
from types import SimpleNamespace
import pytest
from bank_mineru_mcp import processing_guest


@pytest.mark.parametrize('exc,code', [
    (MemoryError(), 'DOCUMENT_RESOURCE_EXHAUSTED'),
    (OSError(errno.ENOSPC,'private'), 'DOCUMENT_RESOURCE_EXHAUSTED'),
    (PermissionError('private'), 'DOCUMENT_SOURCE_UNAVAILABLE'),
    (FileNotFoundError('private'), 'DOCUMENT_SOURCE_UNAVAILABLE'),
    (OSError(errno.EIO,'private'), 'DOCUMENT_ENGINE_UNAVAILABLE'),
])
def test_guest_preserves_failure_kind_without_private_diagnostic(monkeypatch,capsys,exc,code):
    for name in ('BANK_READING_WORK_ROOT', 'SQLITE_TMPDIR'):
        monkeypatch.setenv(name, os.environ.get(name, ''))
    request={'kind':'parse','source':'source.xlsx','max_bytes':1024**2,'header_row':1}
    monkeypatch.setattr(processing_guest.sys,'stdin',SimpleNamespace(buffer=io.BytesIO(json.dumps(request).encode())))
    def failing(*args,**kwargs):raise exc
    monkeypatch.setattr(processing_guest,'extract_workbook',failing)
    previous = os.umask(0o077)
    try:
        processing_guest.main()
    finally:
        os.umask(previous)
    value=json.loads(capsys.readouterr().out)
    assert value=={'status':'failed','error_code':code}
