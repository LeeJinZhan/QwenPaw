"""MCP tool contracts over opaque task file and document references."""

from __future__ import annotations

import asyncio
import functools
import inspect
import hashlib
import json
import re
import stat
from collections import OrderedDict

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
from .aggregate_contract import argument_detail
from .inventory import inventory_summary, inventory_page
from .native_office import SUPPORTED as _NATIVE_OFFICE
from .schemas import NormalizedDocument
from .normalization import _chunks

_PARSE_METHODS = {"auto", "ocr", "txt"}
_LANGUAGES = {"auto", "zh", "en"}
_STRUCTURED = {".xlsx", ".xls", ".csv", ".tsv"}
_SUPPORTED = {
    ".doc": {"application/msword"},
    ".xls": {"application/vnd.ms-excel"},
    ".ppt": {"application/vnd.ms-powerpoint"},
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
    def __init__(self, code: str, message: str, *, argument_reason: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.argument_error = argument_detail(argument_reason or message) if code == "DOCUMENT_ARGUMENT_INVALID" else {}


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
        parse_timeout_seconds: float = 1800,
        extract_memory_bytes: int = 4 * 1024**3,
        ocr_batch_size: int = 5,
        execution_mode: str = "development",
    ) -> None:
        self.parse_timeout_seconds = parse_timeout_seconds
        self.extract_memory_bytes = extract_memory_bytes
        if type(ocr_batch_size) is not int or not 1 <= ocr_batch_size <= 5:
            raise ValueError("OCR batch size must be an integer from 1 to 5")
        self.ocr_batch_size = ocr_batch_size
        if execution_mode not in {"runtime", "development"}:
            raise ValueError("Invalid execution mode")
        self.execution_mode = execution_mode
        self.file_resolver = file_resolver
        self.mineru_client = mineru_client
        self.document_store = document_store
        self.structured_store = structured_store
        self._document_sources: dict[str, tuple[str, datetime]] = {}
        # Immutable JSON responses, bounded across all tasks served by this
        # listener. Every reuse still checks task authority and source integrity.
        self._query_results: OrderedDict[str, tuple[str, str, datetime, bytes]] = OrderedDict()
        self._query_result_bytes = 0
        subscribe = getattr(file_resolver, 'on_task_revoked', None)
        if callable(subscribe):
            subscribe(self.discard_task_results)
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
                "DOCUMENT_ARGUMENT_INVALID", "documents must contain 1-5 files"
            )
        if parse_method not in _PARSE_METHODS:
            raise ToolContractError("DOCUMENT_ARGUMENT_INVALID", "parse_method is invalid")
        if language not in _LANGUAGES:
            raise ToolContractError("DOCUMENT_ARGUMENT_INVALID", "language is invalid")
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
                    "DOCUMENT_ARGUMENT_INVALID", "document fields are invalid"
                )
            file_id = str(document.get("file_id") or "").strip()
            file_ref = str(document.get("file_ref") or "").strip()
            if file_id in seen:
                raise ToolContractError('DOCUMENT_ARGUMENT_INVALID', 'document fields are invalid')
            if not file_id or not file_ref:
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
        if self.structured_store is not None:
            self.structured_store.purge_expired()
        now = datetime.now(timezone.utc)
        self._document_sources = {ref: source for ref, source in self._document_sources.items() if source[1] > now}
        layout = [
            source
            for source in resolved
            if str(source.extension or "").lower() not in _STRUCTURED | _NATIVE_OFFICE
        ]
        local = [source for source in resolved if source.extension.lower() in _STRUCTURED | _NATIVE_OFFICE]
        local_results = None
        cached_items = {}
        nonces = {}
        from bank_runtime.gateway.document_access import current_document_execution
        execution = current_document_execution()
        if local and execution is not None and self.structured_store is not None:
            from .parse_jobs import source_nonce, file_lock
            for source in local:
                if source.extension.lower() not in _STRUCTURED:
                    continue
                await execution.validate_sources([_source_descriptor(source)])
                nonce = await asyncio.to_thread(source_nonce, self.structured_store, source, header_row=normalized_options["header_row"])
                nonces[source.file_id] = nonce
                lock = source.path.parent / (".reading-cache-" + hashlib.sha256(nonce).hexdigest() + ".lock")
                async with file_lock(lock):
                    handle = self.structured_store.cached(nonce, source.task_id)
                    if handle is not None:
                        cached_items[source.file_id] = self._structured_item(source, handle, self.structured_store.inventory(handle.document_ref))
            local = [source for source in local if source.file_id not in cached_items]
        if local and execution is not None:
            try:
                result = await execution.execute({"kind": "parse", "sources": [_source_descriptor(source) for source in local]})
                local_results = {item["file_id"]: item for item in result.get("items", [])}
            except Exception as exc:
                local_results = {source.file_id: {"status": "failed", "error_code": getattr(exc, "code", "DOCUMENT_ENGINE_UNAVAILABLE")} for source in local}
        elif local and self.execution_mode == "runtime":
            local_results = {source.file_id: {"status": "failed", "error_code": "DOCUMENT_ENGINE_UNAVAILABLE"} for source in local}
        normalized = {}
        layout_errors = {}
        for source in layout:
            try:
                if execution is not None:
                    await execution.validate_sources([_source_descriptor(source)])
                raw, upload_stems = await self._remote_parse(
                    source,
                    parse_method=parse_method,
                    language=language,
                    tables=normalized_options["tables"],
                    formulas=normalized_options["formulas"],
                )
                if execution is not None:
                    await execution.validate_sources([_source_descriptor(source)])
                normalized.update(normalize_mineru_result(raw, upload_stems=upload_stems, chunk_chars=3200))
            except MinerUClientError as exc:
                layout_errors[source.file_id] = exc.code
        items: list[dict[str, Any]] = []
        for source, requested in zip(resolved, documents):
            # Recheck after external parsing, including cancellation/revocation.
            self._resolve_authorized_source(requested["file_ref"])
            if source.file_id in cached_items:
                await execution.validate_sources([_source_descriptor(source)])
                items.append(cached_items[source.file_id])
                continue
            native = None
            if local_results is not None and source.extension.lower() in _STRUCTURED | _NATIVE_OFFICE:
                result = local_results.get(source.file_id) or {"status": "failed", "error_code": "DOCUMENT_PARSE_FAILED"}
                if result.get("status") != "ready":
                    items.append(_failed_document(source, result.get("error_code", "DOCUMENT_PARSE_FAILED")))
                    continue
                work = None
                try:
                    work = _physical_work(self.document_store.root, source.task_id, result.get("job_id"))
                    if source.extension.lower() in _STRUCTURED:
                        inventory = _read_processing_metadata(work / "inventory.json", max_bytes=16 * 1024**2)
                        inventory["title"] = source.original_name or source.path.name
                        from .parse_jobs import file_lock
                        nonce = nonces[source.file_id]
                        lock = source.path.parent / (".reading-cache-" + hashlib.sha256(nonce).hexdigest() + ".lock")
                        async with file_lock(lock):
                            handle = self._structured().cached(nonce, source.task_id)
                            if handle is None:
                                handle = self._structured().write(source, inventory, work, nonce=nonce)
                        items.append(self._structured_item(source, handle, inventory))
                        continue
                    native = _read_processing_metadata(work / "native.json", max_bytes=32 * 1024**2)
                    if normalized_options["image_text"]:
                        native = await self._ocr_native(source, native, work)
                except Exception as exc:
                    if getattr(exc, "code", "") in {"FILE_ACCESS_DENIED", "FILE_REF_INVALID", "FILE_REF_EXPIRED"}:
                        raise
                    items.append(_failed_document(source, getattr(exc, "code", "DOCUMENT_PARSE_FAILED")))
                    continue
                finally:
                    import shutil
                    if work is not None:
                        shutil.rmtree(work, ignore_errors=True)
            if str(source.extension or "").lower() in _STRUCTURED:
                items.append(await self._parse_structured(source, header_row=normalized_options["header_row"]))
                self._resolve_authorized_source(requested["file_ref"])
                continue
            if source.extension.lower() in _NATIVE_OFFICE:
                if native is None:
                    try:
                        native = await self._parse_native(source)
                    except Exception as exc:
                        items.append(_failed_document(source, getattr(exc, "code", "DOCUMENT_PARSE_FAILED")))
                        continue
                document = NormalizedDocument(title=source.original_name or source.path.name, markdown=native["markdown"],
                    chunks=_chunks(native["markdown"], 3200), page_count=native.get("page_count"))
            elif source.file_id in layout_errors:
                items.append(_failed_document(source, layout_errors[source.file_id]))
                continue
            else:
                document = normalized.get(source.file_id) or NormalizedDocument("", "", (), error_code="MINERU_PARSE_FAILED")
            if document.error_code:
                items.append(_failed_document(source, document.error_code))
                continue
            base = {
                "file_id": source.file_id,
                "status": "completed",
                "media_type": source.media_type,
                "page_count": document.page_count,
                "chunk_count": len(document.chunks),
                "preview": _preview(document.markdown),
                "error_code": None,
                "engine": native["engine"] if native else "mineru",
                "coverage": native["coverage"] if native else {"layout": "parsed", "accuracy": "unverified"},
                "source_inventory": native["source_inventory"] if native else None,
                **({"ocr_batches": native["ocr_batches"]} if native and "ocr_batches" in native else {}),
                **({"image_export": native["image_export"]} if native and "image_export" in native else {}),
            }
            try:
                handle = self.document_store.write(source, document)
            except DocumentStoreError as exc:
                items.append(_failed_document(source, exc.code))
                continue
            if (len(document.markdown) <= self.inline_max_chars
                    and len(json.dumps(document.markdown, ensure_ascii=False).encode("utf-8")) <= 5000):
                items.append(
                    {
                        **base,
                        "content_mode": "inline",
                        "markdown": document.markdown,
                        "document_ref": handle.document_ref,
                    }
                )
            else:
                items.append(
                    {
                        **base,
                        "content_mode": "chunked",
                        "markdown": None,
                        "document_ref": handle.document_ref,
                    }
                )
        for item, requested, source in zip(items, documents, resolved):
            self._resolve_authorized_source(requested["file_ref"])
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
        self._validate_chunk_arguments(cursor, limit)
        ref = str(document_ref or "")
        if ref.startswith("ds1_"):
            try:
                return self._structured().read_chunks(
                    ref,
                    cursor=str(cursor) if cursor is not None else None,
                    limit=int(limit),
                )
            except StructuredStoreError as exc:
                raise ToolContractError(exc.code, str(exc), argument_reason=getattr(exc, "argument_error", {}).get("reason", "")) from exc
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
            raise ToolContractError(exc.code, str(exc), argument_reason=getattr(exc, "argument_error", {}).get("reason", "")) from exc
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
        if format == "cell":
            if (not isinstance(rows, list) or len(rows) != 2 or rows[0] != rows[1] or type(rows[0]) is not int or rows[0] < 1
                    or not isinstance(columns, list) or len(columns) != 1
                    or (row_cursor is not None and (type(row_cursor) is not int or row_cursor < 0))):
                raise ToolContractError("DOCUMENT_ARGUMENT_INVALID", "Cell reading requires one row, one column and a nonnegative character cursor")
            try:
                return self._structured().read_cell(document_ref, sheet=sheet, row=rows[0], column=columns[0], offset=row_cursor or 0)
            except StructuredStoreError as exc:
                raise ToolContractError(exc.code, str(exc), argument_reason=getattr(exc, "argument_error", {}).get("reason", "")) from exc
        if format == "inventory":
            if rows is not None or columns is not None or (row_cursor is not None and (type(row_cursor) is not int or row_cursor < 0)):
                raise ToolContractError("DOCUMENT_ARGUMENT_INVALID", "Inventory uses a nonnegative row_cursor and optional sheet")
            try:
                return {"document_ref": document_ref, "content_mode": "inventory", **inventory_page(
                    self._structured().inventory(document_ref), sheet=sheet, start=row_cursor or 0)}
            except (ValueError, StructuredStoreError) as exc:
                raise ToolContractError(getattr(exc, "code", "DOCUMENT_ARGUMENT_INVALID"), str(exc)) from exc
        if format not in {"markdown", "records", "source"}:
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
            raise ToolContractError(exc.code, str(exc), argument_reason=getattr(exc, "argument_error", {}).get("reason", "")) from exc

    @_bounded_result
    def aggregate(self, document_ref: str, ops: list[dict[str, Any]]) -> dict[str, Any]:
        self._authorize_document(document_ref)
        if not isinstance(ops, list) or not 1 <= len(ops) <= 10:
            raise ToolContractError("DOCUMENT_ARGUMENT_INVALID", "ops must contain 1-10 operations")
        try:
            return self._structured().aggregate(str(document_ref or ""), ops)
        except StructuredStoreError as exc:
            raise ToolContractError(exc.code, str(exc), argument_reason=getattr(exc, "argument_error", {}).get("reason", "")) from exc

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
            raise ToolContractError(exc.code, str(exc), argument_reason=getattr(exc, "argument_error", {}).get("reason", "")) from exc

    @staticmethod
    def _validate_chunk_arguments(cursor, limit):
        # A numeric inventory/row position is not a signed body cursor. Keep
        # opaque tokens untouched so the store still checks their signatures.
        if cursor is not None and (not isinstance(cursor, str) or
                not cursor.startswith(('cur1_', 'cs1_'))):
            raise ToolContractError('DOCUMENT_ARGUMENT_INVALID', 'Invalid chunk cursor',
                                    argument_reason='CHUNK_CURSOR')
        if type(limit) is not int or not 1 <= limit <= 10:
            raise ToolContractError('DOCUMENT_ARGUMENT_INVALID', 'Invalid chunk limit',
                                    argument_reason='CHUNK_LIMIT')

    async def execute_structured_query(self, name, arguments):
        from .parse_jobs import query_job
        ref = arguments.get("document_ref")
        now = datetime.now(timezone.utc)
        for key, (_, _, expiry, encoded) in list(self._query_results.items()):
            if expiry <= now:
                del self._query_results[key]
                self._query_result_bytes -= len(encoded)
        self._authorize_document(ref)
        if name not in {"read_range", "read_document_chunks", "aggregate", "search", "analyze"}:
            raise ToolContractError("FILE_ACCESS_DENIED", "Unregistered query")
        if name == 'read_document_chunks':
            self._validate_chunk_arguments(arguments.get('cursor'), arguments.get('limit', 5))
        cache_key = hashlib.sha256(json.dumps([self._current_task(), name, arguments],
                                              sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        cached = self._query_results.get(cache_key)
        if cached is not None:
            # Validate the durable document too: deleted/expired manifests must
            # not remain readable merely because their response is cached.
            try:
                self._structured().inventory(ref)
            except StructuredStoreError as exc:
                raise ToolContractError(exc.code, str(exc)) from exc
            self._authorize_document(ref)
            self._query_results.move_to_end(cache_key)
            return json.loads(cached[3])
        if name == "read_range":
            mode = arguments.get("format", "markdown")
            rows, columns, cursor = (arguments.get(k) for k in ("rows", "columns", "row_cursor"))
            if mode == "inventory" and (rows is not None or columns is not None or (cursor is not None and (type(cursor) is not int or cursor < 0))):
                raise ToolContractError("DOCUMENT_ARGUMENT_INVALID", "Invalid inventory cursor")
            if mode == "cell" and (not isinstance(rows,list) or len(rows)!=2 or rows[0]!=rows[1] or type(rows[0]) is not int or rows[0]<1 or not isinstance(columns,list) or len(columns)!=1 or (cursor is not None and (type(cursor) is not int or cursor<0))):
                raise ToolContractError("DOCUMENT_ARGUMENT_INVALID", "Invalid cell range")
        try:
            from bank_runtime.gateway.document_access import current_document_execution
            execution = current_document_execution()
            if execution is not None:
                source = self._resolve_authorized_source(self._document_sources[ref][0])
                result = await execution.execute({"kind": "query", "sources": [_source_descriptor(source)]})
            elif self.execution_mode == "runtime" or name == "analyze":
                raise StructuredStoreError("DOCUMENT_ENGINE_UNAVAILABLE", "Runtime physical execution is required")
            else:
                result = await query_job(self._structured(), name, arguments,
                    timeout=min(self.parse_timeout_seconds, 600), memory_bytes=self.extract_memory_bytes)
        except StructuredStoreError as exc:
            raise ToolContractError(exc.code, str(exc), argument_reason=getattr(exc, "argument_error", {}).get("reason", "")) from exc
        except TimeoutError as exc:
            raise ToolContractError("MINERU_TIMEOUT", "Query deadline exceeded") from exc
        except Exception as exc:
            raise ToolContractError(getattr(exc, "code", "DOCUMENT_ENGINE_UNAVAILABLE"), "Physical query failed",
                                    argument_reason=getattr(exc, 'argument_reason', '')) from exc
        self._authorize_document(ref)  # Recheck revocation after external work.
        if len(json.dumps(result, ensure_ascii=False, indent=2).encode()) > 32000:
            raise ToolContractError("DOCUMENT_RESULT_TOO_LARGE", "Query response exceeds budget")
        if result.get("status") != "failed":
            encoded = json.dumps(result, ensure_ascii=False).encode()
            previous = self._query_results.pop(cache_key, None)
            self._query_result_bytes += len(encoded) - (len(previous[3]) if previous else 0)
            self._query_results[cache_key] = (self._current_task(), ref, self._document_sources[ref][1], encoded)
            while len(self._query_results) > 128 or self._query_result_bytes > 2 * 1024**2:
                _, removed = self._query_results.popitem(last=False)
                self._query_result_bytes -= len(removed[3])
        return result

    def discard_task_results(self, task_id: str) -> None:
        """Release cached plaintext at the same boundary as task file refs."""
        for key, (owner, ref, _, encoded) in list(self._query_results.items()):
            if owner == task_id:
                del self._query_results[key]
                self._query_result_bytes -= len(encoded)

    @staticmethod
    def _current_task():
        from bank_runtime.gateway.document_access import current_document_task, DocumentAccessError
        try:
            return current_document_task()
        except DocumentAccessError as exc:
            raise ToolContractError(exc.code, str(exc), argument_reason=getattr(exc, "argument_error", {}).get("reason", "")) from exc

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
        try:
            self._resolve_authorized_source(file_ref[0])
        except ToolContractError as exc:
            for key, (_, ref, _, encoded) in list(self._query_results.items()):
                if ref == document_ref and exc.code in {"FILE_REF_EXPIRED", "FILE_REF_INVALID", "DOCUMENT_REF_EXPIRED"}:
                    del self._query_results[key]
                    self._query_result_bytes -= len(encoded)
            raise

    def _structured(self) -> StructuredStore:
        if self.structured_store is None:
            raise ToolContractError(
                "MINERU_UNAVAILABLE", "Structured document store is unavailable"
            )
        return self.structured_store

    async def _parse_structured(self, source: Any, *, header_row=1) -> dict[str, Any]:
        from .parse_jobs import parse_job
        try:
            handle, inventory = await parse_job(self._structured(), source, timeout=self.parse_timeout_seconds, memory_bytes=self.extract_memory_bytes, header_row=header_row)
        except StructuredStoreError as exc:
            return _failed_document(source, exc.code)
        except TimeoutError:
            return _failed_document(source, "MINERU_TIMEOUT")
        return self._structured_item(source, handle, inventory)

    async def _parse_native(self, source):
        # Explicit development adapter only. Production requires Runtime Docker.
        import tempfile
        from .native_office import extract_native_office
        with tempfile.TemporaryDirectory(prefix="native-", dir=source.path.parent) as work:
            return await asyncio.to_thread(extract_native_office, source.path, Path(work), max_bytes=self.document_store.max_document_bytes)

    async def _ocr_native(self, source, native, work):
        from dataclasses import replace
        assets = native.get("image_assets", [])
        if not isinstance(assets, list) or len(assets) > 10000:
            raise ToolContractError("FILE_ACCESS_DENIED", "Invalid Office image inventory")
        parts = [native["markdown"]]
        markdown_bytes = len(parts[0].encode("utf-8"))
        # Reserve JSONL escaping and metadata as well as plain markdown bytes.
        # Fragment-wise accounting avoids repeatedly encoding an ever-growing
        # document; ten bytes per chunk cover later chunk index growth.
        chunk_bytes, chunk_count, heading_bytes = _ocr_chunk_bytes(parts[0])
        budget = self.document_store.max_document_bytes
        counts = {"batch_size": self.ocr_batch_size,
                  "batches_total": (len(assets) + self.ocr_batch_size - 1) // self.ocr_batch_size,
                  "batches_completed": 0, "batches_failed": 0,
                  "images_exported": len(assets), "images_succeeded": 0, "images_failed": 0}
        seen = set()
        for start in range(0, len(assets), self.ocr_batch_size):
            _check_ocr_expiry(source)
            if max(markdown_bytes, chunk_bytes + chunk_count * heading_bytes) >= budget:
                counts["stop_reason"] = "output_byte_limit"
                break
            images = []
            for asset in assets[start:start + self.ocr_batch_size]:
                leaf = asset.get("name", "") if isinstance(asset, Mapping) else ""
                if (not isinstance(leaf, str) or not re.fullmatch(r"image_[0-9]{3,5}\.png", leaf)
                        or not 1 <= int(leaf[6:-4]) <= 10000 or leaf in seen):
                    raise ToolContractError("FILE_ACCESS_DENIED", "Invalid Office image")
                seen.add(leaf)
                index = int(leaf[6:-4])
                path = work / leaf
                try:
                    metadata = path.lstat()
                    if not stat.S_ISREG(metadata.st_mode):
                        raise ToolContractError("FILE_ACCESS_DENIED", "Invalid Office image")
                    with path.open("rb") as stream:
                        digest = hashlib.file_digest(stream, "sha256").hexdigest()
                except OSError as exc:
                    raise ToolContractError("FILE_REF_INVALID", "Office image unavailable") from exc
                if digest != asset.get("sha256"):
                    raise ToolContractError("FILE_REF_INVALID", "Office image integrity changed")
                images.append(replace(source, file_id=source.file_id + f"-img{index}", path=path, extension=".png",
                    media_type="image/png", size_bytes=metadata.st_size, sha256=digest))
            try:
                raw, stems = await self._remote_parse_batch(images, authority_source=source,
                    parse_method="ocr", language="auto", tables=True, formulas=True)
            except MinerUClientError as exc:
                _check_ocr_expiry(source)
                counts["batches_failed"] += 1
                counts["images_failed"] += len(images)
                # Timed-out upstream work may still exist: never resubmit it,
                # and avoid starting another batch under an exhausted deadline.
                if exc.code in {"MINERU_TIMEOUT", "MINERU_SUBMIT_AMBIGUOUS"}:
                    counts["stop_reason"] = exc.code
                    break
                continue
            _check_ocr_expiry(source)
            counts["batches_completed"] += 1
            normalized = normalize_mineru_result(raw, upload_stems=stems)
            for image in images:
                document = normalized.get(image.file_id)
                if document is None or document.error_code:
                    counts["images_failed"] += 1
                    continue
                original_index = int(image.path.stem[6:])
                fragment = f"\n\n### 原件图片 {original_index} OCR\n" + document.markdown
                size = len(fragment.encode("utf-8"))
                serialized_size, fragment_chunks, fragment_heading = _ocr_chunk_bytes(fragment)
                next_heading = max(heading_bytes, fragment_heading)
                serialized_total = chunk_bytes + serialized_size + (chunk_count + fragment_chunks) * next_heading
                if max(markdown_bytes + size, serialized_total) > budget:
                    counts["stop_reason"] = "output_byte_limit"
                    break
                parts.append(fragment)
                markdown_bytes += size
                chunk_bytes += serialized_size
                chunk_count += fragment_chunks
                heading_bytes = next_heading
                counts["images_succeeded"] += 1
            if "stop_reason" in counts:
                break
        completed = counts["images_succeeded"]
        if "stop_reason" in counts:
            counts["images_unprocessed"] = len(assets) - completed - counts["images_failed"]
        coverage = dict(native["coverage"])
        coverage["image_text"] = "parsed" if assets and completed == len(assets) else "partial" if completed else coverage["image_text"]
        # Missing exported assets remain a coverage gap, even if all exported ones succeeded.
        image_count = native["source_inventory"].get("images")
        if not native["source_inventory"].get("source_inventory_complete") or image_count is None:
            coverage["image_text"] = "partial" if completed else "unknown"
        elif completed < image_count:
            coverage["image_text"] = "partial" if completed else "unread"
        if any(str(item).startswith('native_image_') for item in native.get('diagnostics', [])):
            coverage['image_text'] = 'partial' if completed else coverage['image_text']
        exported = native.get("image_export", {})
        if (any(exported.get(key, 0) for key in ("skipped_count_limit", "skipped_byte_limit", "skipped_pixel_limit", "skipped_unreadable"))
                or exported.get("candidates_total", len(assets)) > len(assets)):
            coverage["image_text"] = "partial" if completed else "unread"
        return {**native, "markdown": "".join(parts), "coverage": coverage, "ocr_batches": counts,
                "source_inventory": {**native["source_inventory"], "image_assets": len(assets), "images_ocr": completed,
                    "images_remaining": max(0, image_count - completed)
                        if native["source_inventory"].get("source_inventory_complete") and type(image_count) is int else None}}

    async def _remote_parse(self, source, *, authority_source=None, **options):
        return await self._remote_parse_batch([source], authority_source=authority_source or source, **options)

    async def _remote_parse_batch(self, sources, *, authority_source, **options):
        """Bound remote waiting by the unchanged grant and task deadline.

        Cancelling the local request is supported. Upstream task deletion is
        not assumed because the installed MinerU API has not been verified.
        """
        from bank_runtime.gateway.document_access import current_document_execution
        execution = current_document_execution()
        authority = authority_source
        if getattr(authority, "task_id", self._current_task()) != self._current_task():
            raise ToolContractError("FILE_ACCESS_DENIED", "File belongs to another task")
        remaining = (authority.expires_at - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0:
            raise MinerUClientError("MINERU_TIMEOUT", "Source execution expired")
        if execution is not None:
            await execution.validate_sources([_source_descriptor(authority)])
        remaining = (authority.expires_at - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0:
            raise MinerUClientError("MINERU_TIMEOUT", "Source execution expired")
        running = asyncio.create_task(self.mineru_client.parse(sources, **options))
        try:
            async with asyncio.timeout(min(remaining, self.parse_timeout_seconds)):
                while True:
                    done, _ = await asyncio.wait([running], timeout=10)
                    if running in done:
                        result = await running
                        if execution is not None:
                            await execution.validate_sources([_source_descriptor(authority)])
                        return result
                    if execution is not None:
                        await execution.validate_sources([_source_descriptor(authority)])
        except TimeoutError as exc:
            await _recheck_remote_failure(execution, authority, "MINERU_TIMEOUT")
            raise MinerUClientError("MINERU_TIMEOUT", "Remote parsing deadline exceeded") from exc
        except MinerUClientError as exc:
            await _recheck_remote_failure(execution, authority, exc.code)
            raise
        except Exception as exc:
            if getattr(exc, "code", "") in {"FILE_ACCESS_DENIED", "FILE_REF_INVALID", "FILE_REF_EXPIRED"}:
                raise
            # Once parse starts, transport or unclassified failures cannot prove
            # that upstream work did not run. This also covers source-recheck
            # outages during polling and after an upstream result returned.
            raise MinerUClientError("MINERU_SUBMIT_AMBIGUOUS", "Remote parsing state is unknown") from exc
        finally:
            if not running.done():
                running.cancel()
            await asyncio.gather(running, return_exceptions=True)

    def _structured_item(self, source, handle, inventory):
        preview = "; ".join(
            f"{sheet['name']}({sheet['rows']}行×{sheet['cols']}列)"
            for sheet in inventory["sheets"][:10]
        )
        return {
            "file_id": source.file_id,
            "status": "completed",
            "media_type": source.media_type,
            "page_count": inventory["sheet_count"],
            "chunk_count": handle.chunk_count,
            "content_mode": "structured",
            "inventory": inventory_summary(inventory),
            "markdown": None,
            "document_ref": handle.document_ref,
            "preview": _preview(preview),
            "error_code": None,
        }


def _check_ocr_expiry(source):
    if source.expires_at <= datetime.now(timezone.utc):
        raise ToolContractError("FILE_REF_EXPIRED", "Office source execution expired")


async def _recheck_remote_failure(execution, authority, upstream_code):
    if execution is None:
        return
    try:
        await execution.validate_sources([_source_descriptor(authority)])
    except Exception as exc:
        # Policy rejection remains authoritative. A transport outage supplies
        # no new upstream state, so it must not erase an ambiguous submission
        # or timeout and thereby allow the same remote task to be submitted again.
        if (getattr(exc, "code", "") in {"FILE_ACCESS_DENIED", "FILE_REF_INVALID", "FILE_REF_EXPIRED"}
                or upstream_code not in {"MINERU_TIMEOUT", "MINERU_SUBMIT_AMBIGUOUS"}):
            raise


def _ocr_chunk_bytes(markdown):
    chunks = _chunks(markdown, 3200)
    # A heading can carry into later chunks when fragments are combined. Bound
    # by every source heading, including ones not at a fragment chunk boundary.
    headings = [match.group(1)[:300] for line in markdown.splitlines()
                if (match := re.match(r"^#{1,6}\s+(.+?)\s*$", line))]
    heading_bytes = max((len(json.dumps(heading, ensure_ascii=False).encode("utf-8")) - 2 for heading in headings), default=0)
    size = sum(len((json.dumps({**asdict(chunk), "heading": ""}, ensure_ascii=False) + "\n").encode("utf-8")) + 10
               for chunk in chunks)
    return size, len(chunks), heading_bytes


def _preview(value: str) -> str:
    # Five-file responses must fit after UTF-8 encoding and JSON escaping.
    text = value[:500]
    while len(json.dumps(text, ensure_ascii=False).encode("utf-8")) > 500:
        text = text[:max(1, len(text) // 2)]
    return text


def _source_descriptor(source):
    digest = getattr(source, "sha256", "")
    if not digest or len(digest) != 64:
        raise ToolContractError("FILE_REF_INVALID", "Source integrity metadata is missing")
    return {"file_id": source.file_id, "sha256": digest, "extension": source.extension.lower()}


def _read_processing_metadata(path, *, max_bytes):
    if path.is_symlink():
        raise ToolContractError('FILE_ACCESS_DENIED', 'Invalid processing metadata')
    # Bound before allocation and again on the actual read, including a file
    # replaced between stat and read. Row facts remain streamed separately.
    if path.stat().st_size > max_bytes:
        raise ToolContractError('DOCUMENT_RESULT_TOO_LARGE', 'Processing metadata exceeds its memory bound')
    with path.open('rb') as stream:
        payload = stream.read(max_bytes + 1)
    if len(payload) > max_bytes:
        raise ToolContractError('DOCUMENT_RESULT_TOO_LARGE', 'Processing metadata exceeds its memory bound')
    return json.loads(payload)


def _physical_work(root, task_id, job_id):
    import re
    if not isinstance(job_id, str) or not re.fullmatch(r"[a-f0-9]{32}", job_id):
        raise ToolContractError("FILE_ACCESS_DENIED", "Invalid processing job")
    task = Path(root) / task_id
    parent = task / ".reading-physical"
    path = parent / job_id
    if task.is_symlink() or parent.is_symlink() or path.is_symlink() or path.resolve().parent != parent.resolve():
        raise ToolContractError("FILE_ACCESS_DENIED", "Invalid processing scope")
    return path


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


def _options(value: dict[str, Any] | None) -> dict[str, Any]:
    if value is None:
        return {"tables": True, "formulas": True, "header_row": 1, "image_text": False}
    if not isinstance(value, Mapping) or not set(value).issubset(
        {"tables", "formulas", "header_row", "image_text"}
    ):
        raise ToolContractError("DOCUMENT_ARGUMENT_INVALID", "options are invalid")
    result = {"tables": True, "formulas": True, "header_row": 1, "image_text": False}
    for key, item in value.items():
        headers = item if isinstance(item, dict) else {"*": item}
        if (key == "header_row" and (not headers or len(headers) > 1000 or any(not isinstance(name, str) or not name or type(row) is not int or not 0 <= row <= 1_048_576 for name, row in headers.items()))) or (key != "header_row" and not isinstance(item, bool)):
            raise ToolContractError("DOCUMENT_ARGUMENT_INVALID", "options are invalid")
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
    elif suffix in {".doc", ".ppt", ".xls"}:
        valid = prefix.startswith(bytes.fromhex("d0cf11e0a1b11ae1"))
    if not valid:
        raise ToolContractError(
            "FILE_TYPE_UNSUPPORTED", "file signature is unsupported"
        )


def _translated(exc: Exception, fallback: str) -> ToolContractError:
    default = 'DOCUMENT_ENGINE_UNAVAILABLE' if isinstance(exc, OSError) else fallback
    code = str(getattr(exc, 'code', default) or default)
    return ToolContractError(code, "file reference is unavailable")


__all__ = ["MinerUToolService", "ToolContractError"]
