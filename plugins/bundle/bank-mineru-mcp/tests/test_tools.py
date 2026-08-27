from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bank_mineru_mcp.document_store import DocumentStore
from bank_mineru_mcp.tools import MinerUToolService, ToolContractError
from bank_runtime.sandbox.file_refs import ResolvedTaskFile


class _Resolver:
    def __init__(self, resolved: ResolvedTaskFile) -> None:
        self.resolved = resolved

    def resolve(self, file_ref, *, expected_task_id=None):
        del file_ref, expected_task_id
        return self.resolved


class _Client:
    async def parse(self, files, **options):
        assert len(files) == 1
        assert options["parse_method"] == "ocr"
        return (
            {"results": {"file_file_001": {"md_content": "# 标题\n" + "正文" * 12000}}},
            {"file_001": "file_file_001"},
        )


def _resolved(tmp_path: Path) -> ResolvedTaskFile:
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


@pytest.mark.asyncio
async def test_parse_documents_validates_ref_and_returns_chunked_result(
    tmp_path,
) -> None:
    resolved = _resolved(tmp_path)
    store = DocumentStore(root=tmp_path, process_start_key=b"t" * 32)
    service = MinerUToolService(
        file_resolver=_Resolver(resolved),
        mineru_client=_Client(),
        document_store=store,
        inline_max_chars=20000,
    )

    response = await service.parse_documents(
        documents=[{"file_id": "file_001", "file_ref": "fr1_opaque"}],
        parse_method="ocr",
        language="zh",
        options={"tables": True, "formulas": True},
    )

    assert response["status"] == "completed"
    item = response["items"][0]
    assert item["content_mode"] == "chunked"
    assert item["markdown"] is None
    assert item["document_ref"].startswith("dr1_")
    assert len(item["preview"]) <= 1000

    page = service.read_document_chunks(item["document_ref"], limit=1)
    assert page["chunks"]


@pytest.mark.asyncio
async def test_parse_documents_rejects_mismatched_file_id_and_forbidden_shape(
    tmp_path,
) -> None:
    resolved = _resolved(tmp_path)
    service = MinerUToolService(
        file_resolver=_Resolver(resolved),
        mineru_client=_Client(),
        document_store=DocumentStore(root=tmp_path, process_start_key=b"t" * 32),
    )
    with pytest.raises(ToolContractError) as mismatch:
        await service.parse_documents(
            documents=[{"file_id": "file_other", "file_ref": "fr1_opaque"}],
        )
    assert mismatch.value.code == "FILE_REF_INVALID"

    with pytest.raises(ToolContractError):
        await service.parse_documents(
            documents=[
                {"file_id": "file_001", "file_ref": "fr1", "url": "http://forbidden"}
            ],
        )
