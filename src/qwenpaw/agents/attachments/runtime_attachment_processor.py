"""Convert task-local Runtime attachments into bounded model content.

The processor is a Worker data-plane component.  It accepts only files that
have already been authorized and materialized in a task-private directory; it
does not know about OSS credentials, object keys, URLs, or Runtime headers.
"""
from __future__ import annotations

import html
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from ..tools.runtime_sandbox_oss import (
    PreparedSandboxFile,
    content_part_for_prepared_file,
)


_TEXT_EXTENSIONS = {
    ".txt",
    ".md",
    ".markdown",
    ".json",
    ".jsonl",
    ".csv",
    ".tsv",
    ".xml",
    ".yaml",
    ".yml",
    ".log",
}
_TEXT_MIME_TYPES = {
    "application/json",
    "application/ld+json",
    "application/xml",
    "application/yaml",
    "application/x-yaml",
}
_OOXML_MIME_TO_HANDLER = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
}
_OOXML_EXTENSION_TO_HANDLER = {
    ".docx": "docx",
    ".xlsx": "xlsx",
    ".pptx": "pptx",
}
_MAX_XML_MEMBERS = 4096
_MAX_XML_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
_MAX_PDF_PAGES = 500
_DIGITS = re.compile(r"(\d+)")


@dataclass(frozen=True)
class RuntimeAttachmentProcessingConfig:
    """Worker-side limits for inline attachment content."""

    inline_file_max_chars: int = 128_000
    inline_task_max_chars: int = 384_000

    def __post_init__(self) -> None:
        if self.inline_file_max_chars <= 0:
            raise ValueError("runtime attachment file inline budget must be positive")
        if self.inline_task_max_chars < self.inline_file_max_chars:
            raise ValueError(
                "runtime attachment task inline budget must be greater than or equal to file budget",
            )


class RuntimeAttachmentProcessingError(RuntimeError):
    """Stable internal attachment-processing failure."""

    def __init__(self, file_id: str, reason_code: str) -> None:
        self.file_id = str(file_id or "").strip()
        self.reason_code = str(reason_code or "ATTACHMENT_PROCESSING_FAILED").strip()
        super().__init__(f"Worker attachment processing failed: {self.reason_code}")


@dataclass
class RuntimeAttachmentProcessingResult:
    """Safe processor output; host paths and storage locators are excluded."""

    content_parts: list[dict[str, Any]] = field(default_factory=list)
    safe_attachment_refs: list[dict[str, str]] = field(default_factory=list)
    warnings: list[dict[str, str]] = field(default_factory=list)
    metrics: dict[str, int] = field(
        default_factory=lambda: {
            "file_count": 0,
            "inline_text_chars": 0,
            "truncated_file_count": 0,
        },
    )


class RuntimeAttachmentProcessor:
    """Route and safely process files already materialized by the Worker."""

    def __init__(
        self,
        config: RuntimeAttachmentProcessingConfig | None = None,
    ) -> None:
        self.config = config or RuntimeAttachmentProcessingConfig()

    def route(self, prepared: PreparedSandboxFile) -> str:
        """Resolve a Worker handler using MIME, extension, and local signature."""
        self._validate_prepared(prepared)
        path = prepared.local_path
        suffix = path.suffix.lower()
        content_type = str(prepared.content_type or "").split(";", 1)[0].strip().lower()
        prefix = path.read_bytes()[:512]

        signature_handler = _signature_handler(prefix, path)
        declared_handler = _declared_handler(content_type, suffix)
        if declared_handler in {"pdf", "docx", "xlsx", "pptx"}:
            if signature_handler != declared_handler:
                raise RuntimeAttachmentProcessingError(
                    prepared.file_id,
                    "ATTACHMENT_TYPE_MISMATCH",
                )
            return declared_handler
        if signature_handler in {"pdf", "docx", "xlsx", "pptx"}:
            if declared_handler not in {"binary", signature_handler}:
                raise RuntimeAttachmentProcessingError(
                    prepared.file_id,
                    "ATTACHMENT_TYPE_MISMATCH",
                )
            return signature_handler

        if declared_handler == "text":
            if b"\x00" in prefix and not prefix.startswith((b"\xff\xfe", b"\xfe\xff")):
                raise RuntimeAttachmentProcessingError(
                    prepared.file_id,
                    "ATTACHMENT_TYPE_MISMATCH",
                )
            if signature_handler not in {"binary", "text"}:
                raise RuntimeAttachmentProcessingError(
                    prepared.file_id,
                    "ATTACHMENT_TYPE_MISMATCH",
                )
            return "text"
        if declared_handler in {"image", "audio", "video"}:
            if signature_handler not in {declared_handler, "binary"}:
                raise RuntimeAttachmentProcessingError(
                    prepared.file_id,
                    "ATTACHMENT_TYPE_MISMATCH",
                )
            return declared_handler
        return signature_handler if signature_handler != "text" else "binary"

    def process(
        self,
        prepared_files: list[PreparedSandboxFile],
    ) -> RuntimeAttachmentProcessingResult:
        result = RuntimeAttachmentProcessingResult()
        remaining = self.config.inline_task_max_chars
        for prepared in prepared_files:
            handler = self.route(prepared)
            result.metrics["file_count"] += 1
            safe_ref = {
                "file_id": prepared.file_id,
                "display_name": prepared.original_name,
                "content_type": prepared.content_type,
                "handler": handler,
            }
            result.safe_attachment_refs.append(safe_ref)

            if handler in {"image", "audio", "video"}:
                result.content_parts.append(content_part_for_prepared_file(prepared))
                continue
            if handler == "binary":
                result.content_parts.append(
                    _safe_status_part(
                        prepared,
                        "The attachment is available only to an applicable Worker Skill; it was not inlined.",
                    ),
                )
                result.warnings.append(
                    _warning(prepared, "ATTACHMENT_INLINE_UNSUPPORTED"),
                )
                continue

            extracted = self._extract(handler, prepared)
            if not extracted:
                result.content_parts.append(
                    _safe_status_part(prepared, "The attachment is empty."),
                )
                result.warnings.append(_warning(prepared, "ATTACHMENT_EMPTY"))
                continue

            allowance = min(self.config.inline_file_max_chars, remaining)
            inlined = extracted[:allowance]
            truncated = len(extracted) > len(inlined)
            if truncated:
                _write_task_local_derived_text(prepared, extracted)
                safe_ref["derived_ref"] = f"derived:{prepared.file_id}"
                result.metrics["truncated_file_count"] += 1
                result.warnings.append(
                    _warning(prepared, "ATTACHMENT_TEXT_TRUNCATED"),
                )
            result.content_parts.append(
                _untrusted_text_part(prepared, inlined, truncated=truncated),
            )
            result.metrics["inline_text_chars"] += len(inlined)
            remaining = max(remaining - len(inlined), 0)
        return result

    def _extract(self, handler: str, prepared: PreparedSandboxFile) -> str:
        try:
            if handler == "text":
                return _decode_text(prepared.local_path.read_bytes())
            if handler == "pdf":
                return _extract_pdf(prepared.local_path)
            if handler == "docx":
                return _extract_docx(prepared.local_path)
            if handler == "xlsx":
                return _extract_xlsx(prepared.local_path)
            if handler == "pptx":
                return _extract_pptx(prepared.local_path)
        except RuntimeAttachmentProcessingError:
            raise
        except UnicodeError as error:
            raise RuntimeAttachmentProcessingError(
                prepared.file_id,
                "ATTACHMENT_TEXT_DECODE_FAILED",
            ) from error
        except (OSError, ValueError, zipfile.BadZipFile, ElementTree.ParseError) as error:
            raise RuntimeAttachmentProcessingError(
                prepared.file_id,
                f"ATTACHMENT_{handler.upper()}_PARSE_FAILED",
            ) from error
        raise RuntimeAttachmentProcessingError(
            prepared.file_id,
            "ATTACHMENT_HANDLER_UNAVAILABLE",
        )

    @staticmethod
    def _validate_prepared(prepared: PreparedSandboxFile) -> None:
        if not isinstance(prepared, PreparedSandboxFile):
            raise TypeError("processor requires PreparedSandboxFile")
        if not prepared.local_path.is_file():
            raise RuntimeAttachmentProcessingError(
                prepared.file_id,
                "ATTACHMENT_LOCAL_FILE_MISSING",
            )


def _declared_handler(content_type: str, suffix: str) -> str:
    if content_type.startswith("text/") or content_type in _TEXT_MIME_TYPES or suffix in _TEXT_EXTENSIONS:
        return "text"
    if content_type.startswith("image/"):
        return "image"
    if content_type.startswith("audio/"):
        return "audio"
    if content_type.startswith("video/"):
        return "video"
    if content_type == "application/pdf" or suffix == ".pdf":
        return "pdf"
    if content_type in _OOXML_MIME_TO_HANDLER:
        return _OOXML_MIME_TO_HANDLER[content_type]
    if suffix in _OOXML_EXTENSION_TO_HANDLER:
        return _OOXML_EXTENSION_TO_HANDLER[suffix]
    return "binary"


def _signature_handler(prefix: bytes, path: Path) -> str:
    if prefix.startswith(b"%PDF-"):
        return "pdf"
    if prefix.startswith(b"\x89PNG\r\n\x1a\n") or prefix.startswith((b"\xff\xd8\xff", b"GIF87a", b"GIF89a")):
        return "image"
    if prefix.startswith(b"RIFF") and prefix[8:12] == b"WAVE":
        return "audio"
    if len(prefix) >= 12 and prefix[4:8] == b"ftyp":
        return "video"
    if prefix.startswith(b"PK\x03\x04"):
        return _ooxml_handler(path)
    if not prefix or _looks_like_text(prefix):
        return "text"
    return "binary"


def _ooxml_handler(path: Path) -> str:
    try:
        with _safe_zip(path) as archive:
            names = set(archive.namelist())
            if "word/document.xml" in names:
                return "docx"
            if "xl/workbook.xml" in names:
                return "xlsx"
            if "ppt/presentation.xml" in names:
                return "pptx"
    except (OSError, zipfile.BadZipFile, ValueError):
        return "binary"
    return "binary"


def _looks_like_text(content: bytes) -> bool:
    if b"\x00" in content and not content.startswith((b"\xff\xfe", b"\xfe\xff")):
        return False
    try:
        _decode_text(content)
    except UnicodeError:
        return False
    return True


def _decode_text(content: bytes) -> str:
    if not content:
        return ""
    if b"\x00" in content and not content.startswith((b"\xff\xfe", b"\xfe\xff")):
        raise UnicodeError("binary NUL in text")
    if content.startswith(b"\xef\xbb\xbf"):
        return content.decode("utf-8-sig")
    if content.startswith((b"\xff\xfe", b"\xfe\xff")):
        return content.decode("utf-16")
    for encoding in ("utf-8", "gb18030"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise UnicodeError("unsupported text encoding")


def _safe_zip(path: Path) -> zipfile.ZipFile:
    archive = zipfile.ZipFile(path, "r")
    infos = archive.infolist()
    if len(infos) > _MAX_XML_MEMBERS:
        archive.close()
        raise ValueError("Office package has too many members")
    total_size = 0
    for info in infos:
        total_size += info.file_size
        normalized = Path(info.filename)
        if (
            info.flag_bits & 0x1
            or info.filename.startswith(("/", "\\"))
            or ".." in normalized.parts
            or info.filename.lower().endswith("vbaproject.bin")
            or "/embeddings/" in f"/{info.filename.lower()}"
        ):
            archive.close()
            raise ValueError("Office package contains a forbidden member")
    if total_size > _MAX_XML_UNCOMPRESSED_BYTES:
        archive.close()
        raise ValueError("Office package is too large after decompression")
    return archive


def _xml(archive: zipfile.ZipFile, member: str) -> ElementTree.Element:
    content = archive.read(member)
    lowered = content.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise ValueError("Office XML declarations are not allowed")
    return ElementTree.fromstring(content)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].rsplit(":", 1)[-1]


def _element_texts(root: ElementTree.Element, names: set[str] | None = None) -> list[str]:
    accepted = names or {"t"}
    return [
        (node.text or "").strip()
        for node in root.iter()
        if _local_name(node.tag) in accepted and (node.text or "").strip()
    ]


def _extract_docx(path: Path) -> str:
    with _safe_zip(path) as archive:
        root = _xml(archive, "word/document.xml")
        lines: list[str] = []
        for node in root.iter():
            name = _local_name(node.tag)
            if name not in {"p", "tr"}:
                continue
            values = _element_texts(node)
            if values:
                separator = " | " if name == "tr" else ""
                lines.append(separator.join(values))
        return "\n".join(dict.fromkeys(lines))


def _extract_xlsx(path: Path) -> str:
    with _safe_zip(path) as archive:
        names = set(archive.namelist())
        shared_strings: list[str] = []
        if "xl/sharedStrings.xml" in names:
            shared_root = _xml(archive, "xl/sharedStrings.xml")
            for item in shared_root:
                shared_strings.append("".join(_element_texts(item)))

        relationships: dict[str, str] = {}
        rel_member = "xl/_rels/workbook.xml.rels"
        if rel_member in names:
            for rel in _xml(archive, rel_member):
                rel_id = str(rel.attrib.get("Id") or "")
                target = str(rel.attrib.get("Target") or "")
                if rel_id and target and "://" not in target and not target.startswith("/"):
                    relationships[rel_id] = "xl/" + target.lstrip("/")

        workbook = _xml(archive, "xl/workbook.xml")
        sheets: list[tuple[str, str]] = []
        for sheet in workbook.iter():
            if _local_name(sheet.tag) != "sheet" or sheet.attrib.get("state") in {"hidden", "veryHidden"}:
                continue
            rel_id = next((value for key, value in sheet.attrib.items() if _local_name(key) == "id"), "")
            member = relationships.get(rel_id, "")
            if member in names:
                sheets.append((str(sheet.attrib.get("name") or "Sheet"), member))

        lines: list[str] = []
        for sheet_name, member in sheets:
            lines.append(f"[Worksheet: {sheet_name}]")
            root = _xml(archive, member)
            for cell in root.iter():
                if _local_name(cell.tag) != "c":
                    continue
                ref = str(cell.attrib.get("r") or "cell")
                cell_type = str(cell.attrib.get("t") or "")
                formula = next((node.text or "" for node in cell if _local_name(node.tag) == "f"), "")
                inline_text = "".join(
                    _element_texts(cell),
                ) if cell_type == "inlineStr" else ""
                value = next((node.text or "" for node in cell if _local_name(node.tag) == "v"), "")
                if cell_type == "s" and value.isdigit() and int(value) < len(shared_strings):
                    value = shared_strings[int(value)]
                display = inline_text or value
                if formula:
                    display = f"={formula}" + (f" -> {display}" if display else "")
                if display:
                    lines.append(f"{ref}={display}")
        return "\n".join(lines)


def _natural_key(value: str) -> list[int | str]:
    return [int(part) if part.isdigit() else part for part in _DIGITS.split(value)]


def _extract_pptx(path: Path) -> str:
    with _safe_zip(path) as archive:
        names = archive.namelist()
        slide_members = sorted(
            (
                name
                for name in names
                if name.startswith("ppt/slides/slide") and name.endswith(".xml")
            ),
            key=_natural_key,
        )
        note_members = sorted(
            (
                name
                for name in names
                if name.startswith("ppt/notesSlides/notesSlide") and name.endswith(".xml")
            ),
            key=_natural_key,
        )
        lines: list[str] = []
        for index, member in enumerate(slide_members, start=1):
            text = " ".join(_element_texts(_xml(archive, member)))
            if text:
                lines.append(f"[Slide {index}] {text}")
        for index, member in enumerate(note_members, start=1):
            text = " ".join(_element_texts(_xml(archive, member)))
            if text:
                lines.append(f"[Notes {index}] {text}")
        return "\n".join(lines)


def _extract_pdf(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as error:  # pragma: no cover - deployment preflight
        raise RuntimeAttachmentProcessingError(
            "",
            "ATTACHMENT_PDF_PARSER_UNAVAILABLE",
        ) from error
    reader = PdfReader(str(path), strict=False)
    if reader.is_encrypted:
        raise ValueError("encrypted PDF is not supported")
    if len(reader.pages) > _MAX_PDF_PAGES:
        raise ValueError("PDF page count exceeds Worker limit")
    lines = [f"[PDF pages: {len(reader.pages)}]"]
    for page_number, page in enumerate(reader.pages, start=1):
        text = str(page.extract_text() or "").strip()
        if text:
            lines.append(f"[Page {page_number}]\n{text}")
    return "\n".join(lines)


def _write_task_local_derived_text(
    prepared: PreparedSandboxFile,
    content: str,
) -> None:
    target_dir = prepared.local_path.parent / ".worker-derived"
    target_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = target_dir / f"{prepared.file_id}.txt"
    target.write_text(content, encoding="utf-8")
    target.chmod(0o600)


def _untrusted_text_part(
    prepared: PreparedSandboxFile,
    content: str,
    *,
    truncated: bool,
) -> dict[str, str]:
    suffix = "\n[Worker inline content truncated.]" if truncated else ""
    safe_name = html.escape(prepared.original_name, quote=True)
    safe_content_type = html.escape(prepared.content_type, quote=True)
    return {
        "type": "text",
        "text": (
            f'<runtime-attachment name="{safe_name}" '
            f'content_type="{safe_content_type}" trust="untrusted">\n'
            f"{content}{suffix}\n</runtime-attachment>"
        ),
    }


def _safe_status_part(
    prepared: PreparedSandboxFile,
    status: str,
) -> dict[str, str]:
    return {
        "type": "text",
        "text": f"Attachment {html.escape(prepared.original_name, quote=True)!r}: {status}",
    }


def _warning(
    prepared: PreparedSandboxFile,
    reason_code: str,
) -> dict[str, str]:
    return {
        "file_id": prepared.file_id,
        "display_name": prepared.original_name,
        "reason_code": reason_code,
    }
