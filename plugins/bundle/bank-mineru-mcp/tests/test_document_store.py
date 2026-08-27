from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bank_mineru_mcp.document_store import DocumentStore, DocumentStoreError
from bank_mineru_mcp.schemas import NormalizedChunk, NormalizedDocument
from bank_runtime.sandbox.file_refs import ResolvedTaskFile


def _source(tmp_path: Path) -> ResolvedTaskFile:
    task_root = tmp_path / "task_001"
    task_root.mkdir()
    path = task_root / "file_001.pdf"
    path.write_bytes(b"%PDF-1.7")
    return ResolvedTaskFile(
        task_id="task_001",
        file_id="file_001",
        path=path,
        media_type="application/pdf",
        extension=".pdf",
        size_bytes=8,
        sha256="0" * 64,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )


def _document() -> NormalizedDocument:
    return NormalizedDocument(
        title="年度报告",
        markdown="正文" * 5000,
        chunks=(
            NormalizedChunk(index=0, heading="第一章", text="第一段"),
            NormalizedChunk(index=1, heading="第二章", text="第二段"),
        ),
        page_count=2,
    )


def test_document_store_writes_bounded_task_result_and_reads_opaque_cursor(
    tmp_path,
) -> None:
    store = DocumentStore(root=tmp_path, process_start_key=b"d" * 32)
    handle = store.write(_source(tmp_path), _document())

    page = store.read_chunks(handle.document_ref, cursor=None, limit=1)
    assert handle.document_ref.startswith("dr1_")
    assert page.chunks[0].text == "第一段"
    assert page.has_more is True
    assert page.next_cursor and page.next_cursor.startswith("cur1_")
    second = store.read_chunks(handle.document_ref, cursor=page.next_cursor, limit=10)
    assert [chunk.index for chunk in second.chunks] == [1]
    assert second.has_more is False
    assert ".mineru" in str(handle.path)

    store.delete_task("task_001")
    with pytest.raises(DocumentStoreError, match="expired"):
        store.read_chunks(handle.document_ref, cursor=None, limit=1)


def test_document_store_rejects_result_over_per_document_limit(tmp_path) -> None:
    store = DocumentStore(
        root=tmp_path,
        process_start_key=b"d" * 32,
        max_document_bytes=10,
        max_task_bytes=20,
    )
    with pytest.raises(DocumentStoreError) as failed:
        store.write(_source(tmp_path), _document())
    assert failed.value.code == "DOCUMENT_RESULT_TOO_LARGE"
