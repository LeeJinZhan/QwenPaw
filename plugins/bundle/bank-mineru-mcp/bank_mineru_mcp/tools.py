"""MCP tool contracts over opaque task file and document references."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import zipfile
from typing import Any, Mapping

from .document_store import DocumentStore, DocumentStoreError
from .mineru_client import MinerUClientError
from .normalization import normalize_mineru_result

_PARSE_METHODS = {"auto", "ocr", "txt"}
_LANGUAGES = {"auto", "zh", "en"}
_SUPPORTED = {
    ".pdf": {"application/pdf"},
    ".png": {"image/png"},
    ".jpg": {"image/jpeg"},
    ".jpeg": {"image/jpeg"},
    ".docx": {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    },
    ".pptx": {
        "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    },
    ".xlsx": {"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
}


class ToolContractError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class MinerUToolService:
    def __init__(
        self,
        *,
        file_resolver: Any,
        mineru_client: Any,
        document_store: DocumentStore,
        inline_max_chars: int = 20_000,
    ) -> None:
        self.file_resolver = file_resolver
        self.mineru_client = mineru_client
        self.document_store = document_store
        self.inline_max_chars = max(1_000, min(int(inline_max_chars), 100_000))

    async def parse_documents(
        self,
        documents: list[dict[str, Any]],
        parse_method: str = "auto",
        language: str = "auto",
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(documents, list) or not 1 <= len(documents) <= 5:
            raise ToolContractError(
                "FILE_REF_INVALID", "documents must contain 1-5 files"
            )
        if parse_method not in _PARSE_METHODS:
            raise ToolContractError("FILE_REF_INVALID", "parse_method is invalid")
        if language not in _LANGUAGES:
            raise ToolContractError("FILE_REF_INVALID", "language is invalid")
        normalized_options = _options(options)
        resolved = []
        task_ids: set[str] = set()
        seen: set[str] = set()
        for document in documents:
            if not isinstance(document, Mapping) or set(document) != {
                "file_id",
                "file_ref",
            }:
                raise ToolContractError(
                    "FILE_REF_INVALID", "document fields are invalid"
                )
            file_id = str(document.get("file_id") or "").strip()
            file_ref = str(document.get("file_ref") or "").strip()
            if not file_id or file_id in seen or not file_ref:
                raise ToolContractError(
                    "FILE_REF_INVALID", "document reference is invalid"
                )
            seen.add(file_id)
            try:
                source = self.file_resolver.resolve(file_ref)
            except Exception as exc:
                raise _translated(exc, "FILE_REF_INVALID") from exc
            if source.file_id != file_id:
                raise ToolContractError(
                    "FILE_REF_INVALID", "file_id does not match file_ref"
                )
            _validate_source(source)
            task_ids.add(source.task_id)
            resolved.append(source)
        if len(task_ids) != 1:
            raise ToolContractError(
                "FILE_ACCESS_DENIED", "documents belong to different tasks"
            )
        self.document_store.purge_expired()
        try:
            raw, upload_stems = await self.mineru_client.parse(
                resolved,
                parse_method=parse_method,
                language=language,
                tables=normalized_options["tables"],
                formulas=normalized_options["formulas"],
            )
        except MinerUClientError as exc:
            raise ToolContractError(exc.code, str(exc)) from exc
        normalized = normalize_mineru_result(
            raw,
            upload_stems=upload_stems,
            chunk_chars=2000,
        )
        items: list[dict[str, Any]] = []
        for source in resolved:
            document = normalized[source.file_id]
            if document.error_code:
                items.append(
                    {
                        "file_id": source.file_id,
                        "status": "failed",
                        "media_type": source.media_type,
                        "page_count": None,
                        "chunk_count": 0,
                        "content_mode": None,
                        "markdown": None,
                        "document_ref": None,
                        "preview": "",
                        "error_code": document.error_code,
                    }
                )
                continue
            base = {
                "file_id": source.file_id,
                "status": "completed",
                "media_type": source.media_type,
                "page_count": document.page_count,
                "chunk_count": len(document.chunks),
                "preview": document.markdown[:1000],
                "error_code": None,
            }
            if len(document.markdown) <= self.inline_max_chars:
                items.append(
                    {
                        **base,
                        "content_mode": "inline",
                        "markdown": document.markdown,
                        "document_ref": None,
                    }
                )
            else:
                try:
                    handle = self.document_store.write(source, document)
                except DocumentStoreError as exc:
                    raise ToolContractError(exc.code, str(exc)) from exc
                items.append(
                    {
                        **base,
                        "content_mode": "chunked",
                        "markdown": None,
                        "document_ref": handle.document_ref,
                    }
                )
        completed = sum(item["status"] == "completed" for item in items)
        status = (
            "completed"
            if completed == len(items)
            else "failed" if not completed else "partial"
        )
        return {"status": status, "items": items}

    def read_document_chunks(
        self,
        document_ref: str,
        cursor: str | None = None,
        limit: int = 5,
    ) -> dict[str, Any]:
        try:
            normalized_limit = int(limit)
        except (TypeError, ValueError) as exc:
            raise ToolContractError("FILE_REF_INVALID", "limit is invalid") from exc
        try:
            page = self.document_store.read_chunks(
                str(document_ref or ""),
                cursor=str(cursor) if cursor is not None else None,
                limit=normalized_limit,
            )
        except DocumentStoreError as exc:
            raise ToolContractError(exc.code, str(exc)) from exc
        return {
            "document_ref": page.document_ref,
            "chunks": [asdict(chunk) for chunk in page.chunks],
            "next_cursor": page.next_cursor,
            "has_more": page.has_more,
        }


def _options(value: dict[str, Any] | None) -> dict[str, bool]:
    if value is None:
        return {"tables": True, "formulas": True}
    if not isinstance(value, Mapping) or not set(value).issubset(
        {"tables", "formulas"}
    ):
        raise ToolContractError("FILE_REF_INVALID", "options are invalid")
    result = {"tables": True, "formulas": True}
    for key, item in value.items():
        if not isinstance(item, bool):
            raise ToolContractError("FILE_REF_INVALID", "options are invalid")
        result[key] = item
    return result


def _validate_source(source: Any) -> None:
    suffix = str(source.extension or "").lower()
    allowed_mimes = _SUPPORTED.get(suffix)
    if allowed_mimes is None or str(source.media_type).lower() not in allowed_mimes:
        raise ToolContractError("FILE_TYPE_UNSUPPORTED", "file type is unsupported")
    path = Path(source.path)
    with path.open("rb") as handle:
        prefix = handle.read(16)
    valid = True
    if suffix == ".pdf":
        valid = prefix.startswith(b"%PDF-")
    elif suffix == ".png":
        valid = prefix.startswith(b"\x89PNG\r\n\x1a\n")
    elif suffix in {".jpg", ".jpeg"}:
        valid = prefix.startswith(b"\xff\xd8\xff")
    elif suffix in {".docx", ".pptx", ".xlsx"}:
        valid = zipfile.is_zipfile(path)
    if not valid:
        raise ToolContractError(
            "FILE_TYPE_UNSUPPORTED", "file signature is unsupported"
        )


def _translated(exc: Exception, fallback: str) -> ToolContractError:
    code = str(getattr(exc, "code", fallback) or fallback)
    return ToolContractError(code, "file reference is unavailable")


__all__ = ["MinerUToolService", "ToolContractError"]
