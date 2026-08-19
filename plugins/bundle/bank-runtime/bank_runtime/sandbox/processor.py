"""Convert authorized task-local files into bounded untrusted model blocks."""

from __future__ import annotations

import re
import zipfile
from xml.etree import ElementTree

from agentscope.message import DataBlock, TextBlock
from agentscope.message._block import URLSource

from .cache import PreparedSandboxFile, SandboxCacheError

_TEXT_EXTENSIONS = {
    ".txt",
    ".md",
    ".json",
    ".jsonl",
    ".csv",
    ".tsv",
    ".xml",
    ".yaml",
    ".yml",
    ".log",
}
_OOXML = {".docx", ".xlsx", ".pptx"}
_MAX_ZIP_MEMBERS = 4096
_MAX_ZIP_BYTES = 64 * 1024 * 1024
_MAX_PDF_PAGES = 500


class AttachmentProcessor:
    def __init__(
        self, *, per_file_chars: int = 128_000, task_chars: int = 384_000
    ) -> None:
        self.per_file_chars = max(1, int(per_file_chars))
        self.task_chars = max(self.per_file_chars, int(task_chars))

    def process(self, files: list[PreparedSandboxFile]) -> list[TextBlock | DataBlock]:
        blocks: list[TextBlock | DataBlock] = []
        remaining = self.task_chars
        for prepared in files:
            kind = self._kind(prepared)
            if kind in {"image", "audio", "video"}:
                blocks.append(
                    DataBlock(
                        source=URLSource(
                            url=prepared.local_path.resolve().as_uri(),
                            media_type=prepared.content_type,
                        ),
                        name=prepared.original_name,
                    )
                )
                continue
            if kind == "binary":
                blocks.append(
                    self._status(
                        prepared,
                        "The file requires an approved Worker Skill and was not inlined.",
                    )
                )
                continue
            text = self._extract(kind, prepared)
            allowance = min(self.per_file_chars, remaining)
            rendered = text[:allowance]
            truncated = len(text) > len(rendered)
            remaining = max(remaining - len(rendered), 0)
            blocks.append(
                TextBlock(
                    type="text",
                    text=(
                        f"<runtime_attachment name={prepared.original_name!r} trusted='false'>\n"
                        f"{rendered}\n</runtime_attachment>"
                        + ("\n[Attachment text truncated.]" if truncated else "")
                    ),
                )
            )
        return blocks

    def _kind(self, prepared: PreparedSandboxFile) -> str:
        path = prepared.local_path
        prefix = path.read_bytes()[:512]
        mime = prepared.content_type.split(";", 1)[0].lower()
        suffix = path.suffix.lower()
        if prefix.startswith(b"%PDF-"):
            if (
                mime not in {"application/pdf", "application/octet-stream"}
                and suffix != ".pdf"
            ):
                raise SandboxCacheError("Attachment type mismatch")
            return "pdf"
        if suffix in _OOXML or mime.startswith("application/vnd.openxmlformats"):
            if not zipfile.is_zipfile(path):
                raise SandboxCacheError("Attachment type mismatch")
            if suffix in _OOXML:
                return suffix.removeprefix(".")
            if "wordprocessingml" in mime:
                return "docx"
            if "spreadsheetml" in mime:
                return "xlsx"
            if "presentationml" in mime:
                return "pptx"
            raise SandboxCacheError("Office attachment type is unsupported")
        if mime.startswith("image/"):
            return "image"
        if mime.startswith("audio/"):
            return "audio"
        if mime.startswith("video/"):
            return "video"
        if (
            mime.startswith("text/")
            or mime in {"application/json", "application/xml", "application/yaml"}
            or suffix in _TEXT_EXTENSIONS
        ):
            if b"\x00" in prefix and not prefix.startswith((b"\xff\xfe", b"\xfe\xff")):
                raise SandboxCacheError("Attachment type mismatch")
            return "text"
        return "binary"

    def _extract(self, kind: str, prepared: PreparedSandboxFile) -> str:
        if kind == "text":
            return _decode(prepared.local_path.read_bytes())
        if kind == "pdf":
            from pypdf import PdfReader

            try:
                reader = PdfReader(str(prepared.local_path), strict=True)
                if reader.is_encrypted:
                    raise SandboxCacheError("PDF is encrypted")
                if len(reader.pages) > _MAX_PDF_PAGES:
                    raise SandboxCacheError("PDF page quota exceeded")
                return "\n\n".join(
                    str(page.extract_text() or "") for page in reader.pages
                )
            except SandboxCacheError:
                raise
            except Exception as exc:
                raise SandboxCacheError("PDF parsing failed") from exc
        return _extract_ooxml(prepared.local_path, kind)

    @staticmethod
    def _status(prepared: PreparedSandboxFile, message: str) -> TextBlock:
        return TextBlock(
            type="text", text=f"Attachment {prepared.original_name!r}: {message}"
        )


def _decode(value: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-16", "gb18030"):
        try:
            return value.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise SandboxCacheError("Attachment text encoding is unsupported")


def _extract_ooxml(path, kind: str) -> str:
    with zipfile.ZipFile(path) as archive:
        members = archive.infolist()
        if (
            len(members) > _MAX_ZIP_MEMBERS
            or sum(item.file_size for item in members) > _MAX_ZIP_BYTES
        ):
            raise SandboxCacheError("Office archive quota exceeded")
        names = []
        for item in members:
            name = item.filename.replace("\\", "/")
            if name.startswith("/") or any(
                part in {"", ".", ".."} for part in name.split("/")
            ):
                raise SandboxCacheError("Office archive path is invalid")
            if _office_member(kind, name):
                names.append(name)
        text: list[str] = []
        for name in sorted(names, key=_natural_key):
            try:
                root = ElementTree.fromstring(archive.read(name))
            except ElementTree.ParseError as exc:
                raise SandboxCacheError("Office XML is invalid") from exc
            values = [
                node.text for node in root.iter() if node.text and node.text.strip()
            ]
            if values:
                text.append(" ".join(values))
        return "\n\n".join(text)


def _office_member(kind: str, name: str) -> bool:
    if kind == "docx":
        return (
            name == "word/document.xml"
            or name.startswith("word/header")
            or name.startswith("word/footer")
        )
    if kind == "xlsx":
        return name == "xl/sharedStrings.xml" or name.startswith("xl/worksheets/sheet")
    return name.startswith("ppt/slides/slide") or name.startswith(
        "ppt/notesSlides/notesSlide"
    )


def _natural_key(value: str) -> list[object]:
    return [int(item) if item.isdigit() else item for item in re.split(r"(\d+)", value)]


__all__ = ["AttachmentProcessor"]
