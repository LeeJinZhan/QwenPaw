"""MCP tool contracts over opaque task file and document references."""

from __future__ import annotations

import functools
import inspect
import json

from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
import zipfile
from typing import Any, Mapping

from .document_store import DocumentStore, DocumentStoreError
from .mineru_client import MinerUClientError
from .normalization import normalize_mineru_result
from .spreadsheet import SpreadsheetExtractError, extract_workbook
from .structured_store import StructuredStore, StructuredStoreError
from .recovery import recovery_hint

_PARSE_METHODS = {"auto", "ocr", "txt"}
_LANGUAGES = {"auto", "zh", "en"}
_STRUCTURED = {".xlsx", ".csv", ".tsv"}
_SUPPORTED = {
    ".csv": {"text/csv", "text/plain", "application/csv"},
    ".tsv": {"text/tab-separated-values", "text/plain"},
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


def _bounded_result(function):
    def check(value):
        if len(json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")) > 32000:
            raise ToolContractError("DOCUMENT_RESULT_TOO_LARGE", "Narrow the sheet, rows, columns or operations and retry")
        return value
    if inspect.iscoroutinefunction(function):
        @functools.wraps(function)
        async def asynchronous(*args, **kwargs):
            return check(await function(*args, **kwargs))
        return asynchronous
    @functools.wraps(function)
    def synchronous(*args, **kwargs):
        return check(function(*args, **kwargs))
    return synchronous


class MinerUToolService:
    def __init__(
        self,
        *,
        file_resolver: Any,
        mineru_client: Any,
        document_store: DocumentStore,
        structured_store: StructuredStore | None = None,
        inline_max_chars: int = 20_000,
    ) -> None:
        self.file_resolver = file_resolver
        self.mineru_client = mineru_client
        self.document_store = document_store
        self.structured_store = structured_store
        self._document_sources: dict[str, tuple[str, datetime]] = {}
        self.inline_max_chars = max(1_000, min(int(inline_max_chars), 100_000))

    @_bounded_result
    async def parse_documents(
        self,
        documents: list[dict[str, Any]],
        parse_method: str = "auto",
        language: str = "auto",
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        task_id = self._current_task()
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
            if source.task_id != task_id:
                raise ToolContractError("FILE_ACCESS_DENIED", "File belongs to another task")
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
        now = datetime.now(timezone.utc)
        self._document_sources = {ref: source for ref, source in self._document_sources.items() if source[1] > now}
        layout = [
            source
            for source in resolved
            if str(source.extension or "").lower() not in _STRUCTURED
        ]
        raw: dict[str, Any] = {}
        upload_stems: dict[str, str] = {}
        if layout:
            try:
                raw, upload_stems = await self.mineru_client.parse(
                    layout,
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
            chunk_chars=3200,
        )
        items: list[dict[str, Any]] = []
        for source, requested in zip(resolved, documents):
            # Recheck after external parsing, including cancellation/revocation.
            self._resolve_authorized_source(requested["file_ref"])
            if str(source.extension or "").lower() in _STRUCTURED:
                items.append(self._parse_structured(source))
                continue
            document = normalized[source.file_id]
            if document.error_code:
                items.append(_failed_document(source, document.error_code))
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
            if (len(document.markdown) <= self.inline_max_chars
                    and len(json.dumps(document.markdown, ensure_ascii=False).encode("utf-8")) <= 5000):
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
                    items.append(_failed_document(source, exc.code))
                    continue
                items.append(
                    {
                        **base,
                        "content_mode": "chunked",
                        "markdown": None,
                        "document_ref": handle.document_ref,
                    }
                )
        for item, requested, source in zip(items, documents, resolved):
            if item.get("document_ref") and item["status"] == "completed":
                self._document_sources[item["document_ref"]] = (requested["file_ref"], source.expires_at)
        completed = sum(item["status"] == "completed" for item in items)
        status = (
            "completed"
            if completed == len(items)
            else "failed" if not completed else "partial"
        )
        return {"status": status, "items": items}

    @_bounded_result
    def read_document_chunks(
        self,
        document_ref: str,
        cursor: str | None = None,
        limit: int = 5,
    ) -> dict[str, Any]:
        self._authorize_document(document_ref)
        ref = str(document_ref or "")
        if ref.startswith("ds1_"):
            try:
                return self._structured().read_chunks(
                    ref,
                    cursor=str(cursor) if cursor is not None else None,
                    limit=int(limit),
                )
            except StructuredStoreError as exc:
                raise ToolContractError(exc.code, str(exc)) from exc
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
            "coverage": (
                {"read": page.coverage[0], "total": page.coverage[1]}
                if page.coverage
                else None
            ),
            "recovery_hint": "restart_null",
        }

    @_bounded_result
    def read_range(
        self,
        document_ref: str,
        sheet: str | None = None,
        rows: list[int] | None = None,
        row_cursor: int | None = None,
        columns: list[str] | None = None,
        format: str = "markdown",
        include_header: bool = True,
    ) -> dict[str, Any]:
        self._authorize_document(document_ref)
        if format not in {"markdown", "records"}:
            raise ToolContractError("DOCUMENT_ARGUMENT_INVALID", "format is invalid")
        normalized_rows = None
        if rows is not None:
            if not isinstance(rows, list) or len(rows) != 2:
                raise ToolContractError("DOCUMENT_ARGUMENT_INVALID", "rows must be [start,end]")
            normalized_rows = tuple(rows)
        try:
            return self._structured().read_range(
                str(document_ref or ""),
                sheet=sheet,
                rows=normalized_rows,
                row_cursor=row_cursor,
                columns=columns,
                format=format,
                include_header=bool(include_header),
            )
        except StructuredStoreError as exc:
            raise ToolContractError(exc.code, str(exc)) from exc

    @_bounded_result
    def aggregate(self, document_ref: str, ops: list[dict[str, Any]]) -> dict[str, Any]:
        self._authorize_document(document_ref)
        if not isinstance(ops, list) or not 1 <= len(ops) <= 10:
            raise ToolContractError("DOCUMENT_ARGUMENT_INVALID", "ops must contain 1-10 operations")
        try:
            return self._structured().aggregate(str(document_ref or ""), ops)
        except StructuredStoreError as exc:
            raise ToolContractError(exc.code, str(exc)) from exc

    @_bounded_result
    def search(
        self,
        document_ref: str,
        query: str,
        sheet: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        self._authorize_document(document_ref)
        try:
            return self._structured().search(
                str(document_ref or ""),
                query=str(query or ""),
                sheet=sheet,
                limit=int(limit),
            )
        except StructuredStoreError as exc:
            raise ToolContractError(exc.code, str(exc)) from exc

    @staticmethod
    def _current_task():
        from bank_runtime.gateway.document_access import current_document_task, DocumentAccessError
        try:
            return current_document_task()
        except DocumentAccessError as exc:
            raise ToolContractError(exc.code, str(exc)) from exc

    def _resolve_authorized_source(self, file_ref):
        task_id = self._current_task()
        try:
            source = self.file_resolver.resolve(file_ref)
        except Exception as exc:
            raise _translated(exc, "FILE_REF_INVALID") from exc
        if source.task_id != task_id:
            raise ToolContractError("FILE_ACCESS_DENIED", "File belongs to another task")
        return source

    def _authorize_document(self, document_ref):
        self._current_task()
        file_ref = self._document_sources.get(document_ref)
        if not file_ref:
            raise ToolContractError("DOCUMENT_REF_EXPIRED", "Parse the authorized source again")
        self._resolve_authorized_source(file_ref[0])

    def _structured(self) -> StructuredStore:
        if self.structured_store is None:
            raise ToolContractError(
                "MINERU_UNAVAILABLE", "Structured document store is unavailable"
            )
        return self.structured_store

    def _parse_structured(self, source: Any) -> dict[str, Any]:
        store = self._structured()
        import uuid as _uuid

        work_dir = (
            self.document_store.root
            / source.task_id
            / f".mineru-struct-tmp-{_uuid.uuid4().hex}"
        )
        try:
            inventory = extract_workbook(source.path, work_dir, stem=source.file_id)
            handle = store.write(source, inventory, work_dir)
        except SpreadsheetExtractError as exc:
            return _failed_document(source, exc.code)
        except StructuredStoreError as exc:
            return _failed_document(source, exc.code)
        finally:
            import shutil as _shutil

            _shutil.rmtree(work_dir, ignore_errors=True)
        preview = "; ".join(
            f"{sheet['name']}({sheet['rows']}行×{sheet['cols']}列)"
            for sheet in inventory["sheets"]
        )
        return {
            "file_id": source.file_id,
            "status": "completed",
            "media_type": source.media_type,
            "page_count": inventory["sheet_count"],
            "chunk_count": handle.chunk_count,
            "content_mode": "structured",
            "inventory": {**inventory, "sheets": [
                {key: value for key, value in sheet.items()
                 if key not in {"file", "block_offsets", "block_rows", "legacy_blocks"}}
                for sheet in inventory["sheets"]
            ]},
            "markdown": None,
            "document_ref": handle.document_ref,
            "preview": preview[:1000],
            "error_code": None,
        }


def _failed_document(source: Any, code: str) -> dict[str, Any]:
    return {
        "file_id": source.file_id,
        "status": "failed",
        "media_type": source.media_type,
        "page_count": None,
        "chunk_count": 0,
        "content_mode": None,
        "markdown": None,
        "document_ref": None,
        "preview": "",
        "error_code": code,
        "recovery_hint": recovery_hint(code),
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
