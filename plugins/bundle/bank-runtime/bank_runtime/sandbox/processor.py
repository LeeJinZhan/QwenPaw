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
                continue
            if kind in _TOOL_REQUIRED:
                file_ref = str((file_refs or {}).get(prepared.file_id) or "").strip()
                if not file_ref:
                    raise SandboxCacheError(
                        "Attachment file reference is required"
                    )
                blocks.append(self._tool_required(prepared, file_ref))
                continue
            if kind == "binary":
                blocks.append(
                    self._status(
                        prepared,
                        "The file requires an approved Worker Skill and was not inlined.",
                    )
                )
                continue
            text = _decode(prepared.local_path.read_bytes())
            allowance = min(self.per_file_chars, remaining)
            rendered = text[:allowance]
            truncated = len(text) > len(rendered)
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

    @staticmethod
    def _tool_required(prepared: PreparedSandboxFile, file_ref: str) -> TextBlock:
        attributes = " ".join(
            (
                f"file_id={quoteattr(prepared.file_id)}",
                f"display_name={quoteattr(prepared.original_name)}",
                f"media_type={quoteattr(prepared.content_type)}",
                f"size_bytes={quoteattr(str(prepared.size_bytes))}",
                'processing="tool_required"',
                f"file_ref={quoteattr(file_ref)}",
                'trust="user_supplied_untrusted_content"',
            )
        )
        return TextBlock(
            type="text",
            text=(
                f"<runtime_attachment {attributes}>\n"
                "该文件正文尚未读取。如当前问题依赖其内容，请选择已授权且支持该类型的文件处理工具。\n"
                "</runtime_attachment>"
            ),
        )

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


__all__ = ["AttachmentProcessor"]
