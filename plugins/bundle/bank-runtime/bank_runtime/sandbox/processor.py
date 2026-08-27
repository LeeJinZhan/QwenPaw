"""Convert authorized task-local files into bounded untrusted model blocks."""

from __future__ import annotations

from collections.abc import Mapping
import zipfile
from xml.sax.saxutils import quoteattr

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
_TOOL_REQUIRED = {"pdf", "docx", "xlsx", "pptx"}
_OOXML_MIME = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}
_IMAGE_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
}


class AttachmentProcessor:
    def __init__(
        self, *, per_file_chars: int = 128_000, task_chars: int = 384_000
    ) -> None:
        self.per_file_chars = max(1, int(per_file_chars))
        self.task_chars = max(self.per_file_chars, int(task_chars))

    def process(
        self,
        files: list[PreparedSandboxFile],
        *,
        file_refs: Mapping[str, str] | None = None,
    ) -> list[TextBlock | DataBlock]:
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
                file_ref = str((file_refs or {}).get(prepared.file_id) or "").strip()
                if file_ref:
                    blocks.append(
                        self._reference_block(
                            prepared,
                            file_ref,
                            processing="native_or_tool",
                            message=(
                                "该媒体已按原生能力提供；如任务需要 OCR、版面或其他专用处理，"
                                "可选择已授权且支持该类型的工具。"
                            ),
                        )
                    )
                continue
            if kind in _TOOL_REQUIRED:
                file_ref = str((file_refs or {}).get(prepared.file_id) or "").strip()
                if not file_ref:
                    raise SandboxCacheError("Attachment file reference is required")
                blocks.append(
                    self._reference_block(
                        prepared,
                        file_ref,
                        processing="tool_required",
                        message=(
                            "该文件正文尚未读取。如当前问题依赖其内容，请选择已授权且支持该类型的文件处理工具。"
                        ),
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
            allowance = min(self.per_file_chars, remaining)
            text, source_truncated = _read_text(prepared.local_path, allowance)
            rendered = text[:allowance]
            truncated = source_truncated or len(text) > len(rendered)
            remaining = max(remaining - len(rendered), 0)
            blocks.append(
                TextBlock(
                    type="text",
                    text=(
                        f"<runtime_attachment name={quoteattr(prepared.original_name)} trusted='false'>\n"
                        f"{rendered}\n</runtime_attachment>"
                        + ("\n[Attachment text truncated.]" if truncated else "")
                    ),
                )
            )
        return blocks

    def _kind(self, prepared: PreparedSandboxFile) -> str:
        path = prepared.local_path
        with path.open("rb") as handle:
            prefix = handle.read(512)
        mime = prepared.content_type.split(";", 1)[0].lower()
        suffix = path.suffix.lower()
        if suffix == ".pdf" or mime == "application/pdf" or prefix.startswith(b"%PDF-"):
            if (
                not prefix.startswith(b"%PDF-")
                or suffix != ".pdf"
                or mime not in {"application/pdf", "application/octet-stream"}
            ):
                raise SandboxCacheError("Attachment type mismatch")
            return "pdf"
        if suffix in _OOXML or mime.startswith("application/vnd.openxmlformats"):
            expected_mime = _OOXML_MIME.get(suffix)
            if (
                expected_mime is None
                or mime not in {expected_mime, "application/octet-stream"}
                or not zipfile.is_zipfile(path)
            ):
                raise SandboxCacheError("Attachment type mismatch")
            return suffix.removeprefix(".")
        if mime.startswith("image/") or suffix in _IMAGE_MIME:
            expected_mime = _IMAGE_MIME.get(suffix)
            if (
                expected_mime is None
                or mime not in {expected_mime, "application/octet-stream"}
                or not _image_signature(suffix, prefix)
            ):
                raise SandboxCacheError("Attachment type mismatch")
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

    @staticmethod
    def _reference_block(
        prepared: PreparedSandboxFile,
        file_ref: str,
        *,
        processing: str,
        message: str,
    ) -> TextBlock:
        attributes = " ".join(
            (
                f"file_id={quoteattr(prepared.file_id)}",
                f"display_name={quoteattr(prepared.original_name)}",
                f"media_type={quoteattr(prepared.content_type)}",
                f"size_bytes={quoteattr(str(prepared.size_bytes))}",
                f"processing={quoteattr(processing)}",
                f"file_ref={quoteattr(file_ref)}",
                'trust="user_supplied_untrusted_content"',
            )
        )
        return TextBlock(
            type="text",
            text=(
                f"<runtime_attachment {attributes}>\n"
                f"{message}\n"
                "</runtime_attachment>"
            ),
        )

    @staticmethod
    def _status(prepared: PreparedSandboxFile, message: str) -> TextBlock:
        return TextBlock(
            type="text", text=f"Attachment {prepared.original_name!r}: {message}"
        )


def _read_text(path, max_chars: int) -> tuple[str, bool]:
    byte_limit = max(8, max(0, int(max_chars)) * 4 + 8)
    with path.open("rb") as handle:
        value = handle.read(byte_limit + 1)
    truncated = len(value) > byte_limit
    value = value[:byte_limit]
    for encoding in ("utf-8-sig", "utf-16", "gb18030"):
        for trim in range(0, min(4, len(value)) + 1):
            candidate = value if trim == 0 else value[:-trim]
            try:
                return candidate.decode(encoding), truncated or trim > 0
            except UnicodeDecodeError:
                continue
    raise SandboxCacheError("Attachment text encoding is unsupported")


def _image_signature(suffix: str, prefix: bytes) -> bool:
    if suffix == ".png":
        return prefix.startswith(b"\x89PNG\r\n\x1a\n")
    if suffix in {".jpg", ".jpeg"}:
        return prefix.startswith(b"\xff\xd8\xff")
    if suffix == ".gif":
        return prefix.startswith((b"GIF87a", b"GIF89a"))
    if suffix == ".webp":
        return prefix.startswith(b"RIFF") and prefix[8:12] == b"WEBP"
    if suffix == ".bmp":
        return prefix.startswith(b"BM")
    return prefix.startswith((b"II*\x00", b"MM\x00*"))


__all__ = ["AttachmentProcessor"]
