from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from dataclasses import replace
import json
import zipfile

from bank_mineru_mcp.document_store import DocumentStore
from bank_mineru_mcp.tools import MinerUToolService, ToolContractError
from bank_runtime.sandbox.file_refs import ResolvedTaskFile
from bank_runtime.gateway.completion import parse_outcomes


@pytest.mark.asyncio
@pytest.mark.parametrize("large_first", [True, False])
async def test_batch_retains_readable_result_when_other_document_exceeds_storage_limit(tmp_path, large_first):
    source = _resolved(tmp_path)
    sources = {"small": replace(source, file_id="small"), "large": replace(source, file_id="large")}

    class Resolver:
        def resolve(self, ref):
            return sources[ref]

    class Client:
        async def parse(self, files, **options):
            return {"results": {"small": {"md_content": "正常正文" * 300}, "large": {"md_content": "大文件" * 2000}}}, {s.file_id: s.file_id for s in files}

    service = MinerUToolService(
        file_resolver=Resolver(), mineru_client=Client(), inline_max_chars=1000,
        document_store=DocumentStore(root=tmp_path, max_document_bytes=5000),
    )
    order = ["large", "small"] if large_first else ["small", "large"]
    result = await service.parse_documents([{"file_id": name, "file_ref": name} for name in order])
    assert result["status"] == "partial"
    items = {item["file_id"]: item for item in result["items"]}
    assert items["large"]["status"] == "failed"
    assert items["large"]["error_code"] == "DOCUMENT_RESULT_TOO_LARGE"
    assert items["large"]["document_ref"] is None
    assert items["large"]["markdown"] is None
    assert items["small"]["status"] == "completed"
    assert dict(parse_outcomes([{"type": "text", "text": json.dumps(result)}])) == {"parse:large": False, "parse:small": True}
    page = service.read_document_chunks(items["small"]["document_ref"])
    assert "".join(chunk["text"] for chunk in page["chunks"]) == "正常正文" * 300


@pytest.mark.asyncio
@pytest.mark.parametrize("extension,mime,signature", [
    ("pdf", "application/pdf", b"%PDF-1.7\n"),
    ("png", "image/png", b"\x89PNG\r\n\x1a\n"),
    ("jpg", "image/jpeg", b"\xff\xd8\xff"),
    ("jpeg", "image/jpeg", b"\xff\xd8\xff"),
    ("docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", None),
    ("xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", None),
    ("pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation", None),
])
@pytest.mark.parametrize("large", [False, True])
async def test_supported_format_inline_and_chunked_reading(tmp_path, extension, mime, signature, large):
    # Signature-valid fixtures and simulated OCR/Office parser output test the
    # common transport/normalization path, not the accuracy of the parser itself.
    source = _resolved(tmp_path)
    path = source.path.with_suffix("." + extension)
    if signature is None:
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("[Content_Types].xml", "<Types/>")
    else:
        path.write_bytes(signature)
    source = replace(source, path=path, extension="." + extension, media_type=mime, size_bytes=path.stat().st_size)
    markdown = "# 合成识别结果\n" + ("中文内容，数值123.45；\n" * (4000 if large else 3))

    class Client:
        async def parse(self, files, **options):
            assert files[0].extension == "." + extension
            return {"results": {"sample": {"md_content": markdown}}}, {"file_001": "sample"}

    service = MinerUToolService(file_resolver=_Resolver(source), mineru_client=Client(), document_store=DocumentStore(root=tmp_path))
    response = await service.parse_documents([{"file_id": "file_001", "file_ref": "test-ref"}])
    item = response["items"][0]
    assert response["status"] == "completed"
    if not large:
        assert item["content_mode"] == "inline"
        assert item["markdown"] == markdown
        return
    assert item["chunk_count"] > 24
    cursor = None
    chunks = []
    while True:
        page = service.read_document_chunks(item["document_ref"], cursor=cursor, limit=10)
        assert service.read_document_chunks(item["document_ref"], cursor=cursor, limit=10) == page
        assert sum(len(chunk["text"]) for chunk in page["chunks"]) <= 20000
        chunks.extend(page["chunks"])
        if not page["has_more"]:
            break
        cursor = page["next_cursor"]
    assert [chunk["index"] for chunk in chunks] == list(range(item["chunk_count"]))
    assert "".join(chunk["text"] for chunk in chunks) == markdown


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_result", [None, {}, {"md_content": "  "}, {"md_content": []}])
async def test_bad_parser_item_does_not_discard_other_file(tmp_path, bad_result):
    source = _resolved(tmp_path)

    class Resolver:
        def resolve(self, ref):
            return replace(source, file_id=ref)

    class Client:
        async def parse(self, files, **options):
            return {"results": {"good": {"md_content": "完整正文"}, "bad": bad_result}}, {s.file_id: s.file_id for s in files}

    service = MinerUToolService(file_resolver=Resolver(), mineru_client=Client(), document_store=DocumentStore(root=tmp_path))
    result = await service.parse_documents([{"file_id": name, "file_ref": name} for name in ("good", "bad")])
    assert result["status"] == "partial"
    assert result["items"][0]["markdown"] == "完整正文"
    assert result["items"][1]["status"] == "failed"
    assert result["items"][1]["error_code"] == "MINERU_PARSE_FAILED"


@pytest.mark.asyncio
async def test_120_row_table_pagination_retry_preserves_all_cells(tmp_path) -> None:
    # Simulated MinerU table output, not a real Excel parser/model integration.
    headers = ["记录", "用户", "日期", "助手", "部门", "岗位", "输入token", "输出token", "总token", "场景", "问题"]
    rows = [
        [str(i), f"user{i % 12}", f"2026-09-{7 + i % 4:02d}",
         f"助手{i % 3}", f"部门{i % 2}", f"岗位{i % 4}",
         str(i), str(i * 2), str(i * 3), "资料问答", "合成问题" * 40]
        for i in range(1, 121)
    ]
    markdown = "\n".join("|" + "|".join(row) + "|" for row in [headers, ["---"] * 11, *rows])

    class TableClient:
        async def parse(self, files, **options):
            return {"results": {"table": {"md_content": markdown}}}, {"file_001": "table"}

    service = MinerUToolService(
        file_resolver=_Resolver(_resolved(tmp_path)), mineru_client=TableClient(),
        document_store=DocumentStore(root=tmp_path), inline_max_chars=1000,
    )
    response = await service.parse_documents([{"file_id": "file_001", "file_ref": "test-ref"}])
    item = response["items"][0]
    assert item["content_mode"] == "chunked"
    cursor = None
    chunks = {}
    while True:
        page = service.read_document_chunks(item["document_ref"], cursor=cursor, limit=2)
        retry = service.read_document_chunks(item["document_ref"], cursor=cursor, limit=2)
        assert retry == page
        for chunk in page["chunks"] + retry["chunks"]:
            chunks[chunk["index"]] = chunk["text"]
        if not page["has_more"]:
            break
        cursor = page["next_cursor"]
    assert sorted(chunks) == list(range(item["chunk_count"]))
    reconstructed = "".join(chunks[index] for index in sorted(chunks))
    assert reconstructed == markdown
    actual = [line.strip("|").split("|") for line in reconstructed.splitlines()[2:]]
    assert actual == rows
    assert len({row[1] for row in actual}) == 12
    assert sum(int(row[8]) for row in actual) == 21780


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
