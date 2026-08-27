from __future__ import annotations

from pathlib import Path
import sys
import zipfile

import pytest
from agentscope.message import TextBlock
from pypdf import PdfWriter

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from bank_runtime.sandbox.cache import PreparedSandboxFile, SandboxCacheError
from bank_runtime.sandbox.processor import AttachmentProcessor
from bank_runtime.session import _sanitize_agent_state


def _prepared(path: Path, *, content_type: str) -> PreparedSandboxFile:
    return PreparedSandboxFile(
        file_id="file_001",
        local_path=path,
        content_type=content_type,
        size_bytes=path.stat().st_size,
        original_name=path.name,
        expires_at="2026-08-19T12:00:00+08:00",
    )


def test_processor_does_not_inspect_office_archive_members(tmp_path) -> None:
    path = tmp_path / "malicious.docx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("../word/document.xml", "<x>secret</x>")
    block = AttachmentProcessor().process(
        [
            _prepared(
                path,
                content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        ],
        file_refs={"file_001": "fr1_office"},
    )[0]
    assert isinstance(block, TextBlock)
    assert "tool_required" in block.text
    assert "secret" not in block.text


def test_processor_rejects_declared_text_with_binary_signature(tmp_path) -> None:
    path = tmp_path / "fake.txt"
    path.write_bytes(b"binary\x00payload")
    with pytest.raises(SandboxCacheError):
        AttachmentProcessor().process([_prepared(path, content_type="text/plain")])


def test_processor_rejects_pdf_and_image_signature_mismatch(tmp_path) -> None:
    fake_pdf = tmp_path / "fake.pdf"
    fake_pdf.write_bytes(b"not a pdf")
    with pytest.raises(SandboxCacheError, match="type mismatch"):
        AttachmentProcessor().process(
            [_prepared(fake_pdf, content_type="application/pdf")],
            file_refs={"file_001": "fr1_fake"},
        )

    wrong_mime = tmp_path / "image.png"
    wrong_mime.write_bytes(b"\x89PNG\r\n\x1a\n")
    with pytest.raises(SandboxCacheError, match="type mismatch"):
        AttachmentProcessor().process(
            [_prepared(wrong_mime, content_type="image/jpeg")]
        )


def test_processor_routes_pdf_to_an_opaque_tool_required_block(tmp_path) -> None:
    path = tmp_path / "oversized.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    with path.open("wb") as handle:
        writer.write(handle)

    prepared = _prepared(path, content_type="application/pdf")
    block = AttachmentProcessor().process(
        [prepared],
        file_refs={"file_001": "fr1_opaque"},
    )[0]

    assert isinstance(block, TextBlock)
    assert 'processing="tool_required"' in block.text
    assert 'file_ref="fr1_opaque"' in block.text
    assert 'file_id="file_001"' in block.text
    assert "正文尚未读取" in block.text


def test_processor_does_not_attempt_to_decrypt_or_extract_pdf_text(tmp_path) -> None:
    path = tmp_path / "encrypted.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.encrypt("secret")
    with path.open("wb") as handle:
        writer.write(handle)

    prepared = _prepared(path, content_type="application/pdf")
    block = AttachmentProcessor().process(
        [prepared],
        file_refs={"file_001": "fr1_encrypted"},
    )[0]
    assert isinstance(block, TextBlock)
    assert "tool_required" in block.text


def test_processor_requires_file_ref_for_complex_documents(tmp_path) -> None:
    path = tmp_path / "report.pdf"
    path.write_bytes(b"%PDF-1.7\n")

    with pytest.raises(SandboxCacheError, match="reference"):
        AttachmentProcessor().process([_prepared(path, content_type="application/pdf")])


def test_managed_session_removes_agentscope_file_data_block(tmp_path) -> None:
    path = tmp_path / "image.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n")
    block = AttachmentProcessor().process([_prepared(path, content_type="image/png")])[
        0
    ]
    state = {
        "state": {
            "context": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "describe"},
                        block.model_dump(mode="json"),
                    ],
                }
            ]
        }
    }
    sanitized = _sanitize_agent_state(state)
    assert sanitized["state"]["context"][0]["content"] == [
        {"type": "text", "text": "describe"}
    ]


def test_image_keeps_native_block_and_exposes_optional_opaque_tool_ref(
    tmp_path,
) -> None:
    path = tmp_path / "image.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n")
    blocks = AttachmentProcessor().process(
        [_prepared(path, content_type="image/png")],
        file_refs={"file_001": "fr1_image"},
    )
    assert len(blocks) == 2
    assert blocks[0].type == "data"
    assert blocks[0].source.media_type == "image/png"
    assert isinstance(blocks[1], TextBlock)
    assert 'processing="native_or_tool"' in blocks[1].text
    assert 'file_ref="fr1_image"' in blocks[1].text
