from __future__ import annotations

from pathlib import Path
import sys
import zipfile

import pytest
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


def test_processor_rejects_office_archive_path_traversal(tmp_path) -> None:
    path = tmp_path / "malicious.docx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("../word/document.xml", "<x>secret</x>")
    with pytest.raises(SandboxCacheError):
        AttachmentProcessor().process(
            [
                _prepared(
                    path,
                    content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            ]
        )


def test_processor_rejects_declared_text_with_binary_signature(tmp_path) -> None:
    path = tmp_path / "fake.txt"
    path.write_bytes(b"binary\x00payload")
    with pytest.raises(SandboxCacheError):
        AttachmentProcessor().process([_prepared(path, content_type="text/plain")])


def test_processor_rejects_pdf_over_page_quota(tmp_path) -> None:
    path = tmp_path / "oversized.pdf"
    writer = PdfWriter()
    for _ in range(501):
        writer.add_blank_page(width=72, height=72)
    with path.open("wb") as handle:
        writer.write(handle)

    with pytest.raises(SandboxCacheError, match="page quota"):
        AttachmentProcessor().process([_prepared(path, content_type="application/pdf")])


def test_processor_normalizes_encrypted_pdf_failure(tmp_path) -> None:
    path = tmp_path / "encrypted.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.encrypt("secret")
    with path.open("wb") as handle:
        writer.write(handle)

    with pytest.raises(SandboxCacheError, match="PDF is encrypted"):
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
