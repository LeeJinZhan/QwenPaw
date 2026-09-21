"""Task-local structured workbook store with disk registry and bounded reads.

Complements DocumentStore: spreadsheets are addressed by sheet and row range,
aggregated server-side, and survive process restarts through on-disk manifests.
Cursors are stateless HMAC tokens so a rejected page can never poison later ones.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import shutil
from statistics import median
import sqlite3
from .aggregation import disk_aggregate
from .aggregate_contract import FUNCTIONS, FILTERS, argument_detail, invalid_ops
from typing import Any, Callable, Iterable

from .schemas import DocumentHandle
from .spreadsheet import CHUNK_SIM_ROWS, SpreadsheetExtractError, SourceRow, render_markdown

_KEY_FILE = ".bank-mineru-struct.key"
_DIR_NAME = ".mineru-struct"
_FILTER_OPS = set(FILTERS)
_METRICS = set(FUNCTIONS)


class StructuredStoreError(RuntimeError):
    def __init__(self, code: str, message: str, *, argument_reason: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.argument_error = argument_detail(argument_reason or message) if code == "DOCUMENT_ARGUMENT_INVALID" else {}


@dataclass(frozen=True)
class _Entry:
    task_id: str
    path: Path
    title: str
    sheet_count: int
    total_rows: int
    expires_at: datetime


class StructuredStore:
    def __init__(
        self,
        *,
        root: str | Path,
        process_start_key: bytes | None = None,
        max_task_bytes: int = 256 * 1024 * 1024,
        max_document_bytes: int = 2 * 1024**3,
        ttl_seconds: int = 604_800,
        page_chars: int = 32_000,
        max_groups: int = 500,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        self.key = process_start_key or _load_key(self.root / _KEY_FILE)
        self.max_task_bytes = max(1024, int(max_task_bytes))
        self.max_document_bytes = min(self.max_task_bytes, max(1024, int(max_document_bytes)))
        self.ttl_seconds = max(60, min(int(ttl_seconds), 604_800))
        self.page_chars = max(2_000, min(int(page_chars), 100_000))
        self.max_groups = max(1, min(int(max_groups), 5_000))
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._documents: dict[str, _Entry] = {}
        self._recover()

    # ---------------------------------------------------------------- registry

    def _recover(self) -> None:
        if not self.root.is_dir():
            return
        for task_root in self.root.iterdir():
            derived = task_root / _DIR_NAME
            if not task_root.is_dir() or derived.is_symlink() or not derived.is_dir():
                continue
            for manifest in derived.glob("*/manifest.json"):
                try:
                    payload = json.loads(manifest.read_text(encoding="utf-8"))
                    stored = payload.pop("sha256", "")
                    digest = hashlib.sha256(
                        json.dumps(payload, sort_keys=True).encode("utf-8")
                    ).hexdigest()
                    if stored != digest:
                        continue
                    expires = datetime.fromisoformat(payload["expires_at"])
                    if expires <= _utc(self.clock()):
                        continue
                    self._documents[payload["document_hash"]] = _Entry(
                        task_id=payload["task_id"],
                        path=manifest.parent,
                        title=payload["title"],
                        sheet_count=payload["sheet_count"],
                        total_rows=payload["total_rows"],
                        expires_at=expires,
                    )
                except (OSError, ValueError, KeyError, json.JSONDecodeError):
                    continue

    def write(self, source: Any, inventory: dict[str, Any], work_dir: Path, *, nonce: bytes | None = None) -> DocumentHandle:
        now = _utc(self.clock())
        expiry = min(_utc(source.expires_at), now + timedelta(seconds=self.ttl_seconds))
        if expiry <= now:
            raise StructuredStoreError("DOCUMENT_REF_EXPIRED", "Document source expired")
        nonce = nonce or secrets.token_bytes(32)
        document_hash = hashlib.sha256(nonce).hexdigest()
        task_root = (self.root / source.task_id).resolve(strict=True)
        target = task_root / _DIR_NAME / document_hash
        if target.exists():
            raise StructuredStoreError("FILE_ACCESS_DENIED", "Derived document path collision")
        target.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(target.parent, 0o700)
        payload = {
            "document_hash": document_hash,
            "task_id": source.task_id,
            "title": inventory["title"],
            "sheet_count": inventory["sheet_count"],
            "total_rows": inventory["total_rows"],
            "expires_at": expiry.isoformat(),
            "inventory": inventory,
        }
        payload["sha256"] = hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode("utf-8")
        ).hexdigest()
        manifest_bytes = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        # Serialize the disk count and atomic publication across workers/restarts.
        with (target.parent / ".quota.lock").open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            used = sum(path.stat().st_size for path in target.parent.rglob("*") if path.is_file())
            incoming = sum(path.stat().st_size for path in work_dir.rglob("*") if path.is_file())
            if used + incoming + len(manifest_bytes) > self.max_task_bytes:
                shutil.rmtree(work_dir)
                raise StructuredStoreError("DOCUMENT_RESULT_TOO_LARGE", "Task structured result quota exceeded")
            (work_dir / "manifest.json").write_bytes(manifest_bytes)
            os.chmod(work_dir, 0o700)
            work_dir.rename(target)
        self._documents[document_hash] = _Entry(
            task_id=source.task_id,
            path=target,
            title=inventory["title"],
            sheet_count=inventory["sheet_count"],
            total_rows=inventory["total_rows"],
            expires_at=expiry,
        )
        return DocumentHandle(
            document_ref=self._token("ds1", nonce),
            path=target,
            title=inventory["title"],
            page_count=inventory["sheet_count"],
            chunk_count=self._chunk_count(inventory),
        )

    def cached(self, nonce: bytes, task_id: str) -> DocumentHandle | None:
        ref = self._token("ds1", nonce)
        key = hashlib.sha256(nonce).hexdigest()
        task_root = (self.root / task_id).resolve(strict=True)
        if task_root.parent != self.root:
            raise StructuredStoreError("FILE_ACCESS_DENIED", "Invalid cached task scope")
        path = task_root / _DIR_NAME / key
        if path.is_symlink() or path.parent.is_symlink():
            raise StructuredStoreError("FILE_ACCESS_DENIED", "Invalid derived path")
        if not path.exists():
            return None
        try:
            payload = json.loads((path / "manifest.json").read_text())
            expiry = datetime.fromisoformat(payload["expires_at"])
            if payload["task_id"] != task_id or payload["document_hash"] != key:
                raise StructuredStoreError("FILE_ACCESS_DENIED", "Cached source scope differs")
            self._documents[key] = _Entry(task_id, path, payload["title"], payload["sheet_count"], payload["total_rows"], expiry)
            entry, manifest = self._entry(ref)
        except (KeyError, ValueError, OSError, StructuredStoreError):
            # Only this operation's cache is removed, while its job lock is held.
            self._documents.pop(key, None)
            shutil.rmtree(path)
            return None
        return DocumentHandle(document_ref=ref, path=entry.path, title=entry.title,
            page_count=entry.sheet_count, chunk_count=self._chunk_count(manifest["inventory"]))

    def inventory(self, document_ref: str) -> dict[str, Any]:
        entry, manifest = self._entry(document_ref)
        return manifest["inventory"]

    # ---------------------------------------------------------------- reads

    def read_range(
        self,
        document_ref: str,
        *,
        sheet: str | None = None,
        rows: tuple[int, int] | None = None,
        row_cursor: int | None = None,
        columns: list[str] | None = None,
        format: str = "markdown",
        include_header: bool = True,
    ) -> dict[str, Any]:
        entry, manifest = self._entry(document_ref)
        sheet_meta = self._sheet(manifest, sheet)
        total = sheet_meta["rows"]
        if format not in {"markdown", "records"}:
            raise StructuredStoreError("DOCUMENT_ARGUMENT_INVALID", "format is invalid")
        if rows is not None:
            _validate_range(rows)
        if row_cursor is not None and (type(row_cursor) is not int or row_cursor < 1):
            raise StructuredStoreError("DOCUMENT_ARGUMENT_INVALID", "row_cursor is invalid")
        start = int(row_cursor or (rows[0] if rows else 1))
        if rows and row_cursor:
            raise StructuredStoreError("DOCUMENT_ARGUMENT_INVALID", "rows and row_cursor conflict")
        if start < 1:
            start = 1
        end = int(rows[1] if rows else total) if rows else None
        selected = self._columns(sheet_meta, columns)
        picked: list[tuple[int, list[Any]]] = []
        header_text = (
            render_markdown(selected["names"], []) if format == "markdown" else ""
        )
        invalid_cells = 0
        size = len(header_text)
        for row_number, values in self._iter_rows(entry, sheet_meta, start):
            if end is not None and row_number > end:
                break
            if row_number > total:
                break
            projected = [values[index] for index in selected["indices"]]
            rendered = json.dumps(projected, ensure_ascii=False) if format == "records" else _md_row(projected)
            if picked and size + len(rendered) + 1 > self.page_chars:
                break
            invalid_cells += len(values.invalid_columns.intersection(selected["indices"]))
            picked.append((row_number, projected))
            size += len(rendered) + 1
        last = picked[-1][0] if picked else start - 1
        has_more = last < total and (end is None or last < end)
        body: dict[str, Any]
        if format == "records":
            body = {
                "columns": selected["names"],
                "records": [
                    {"row": number, "values": values} for number, values in picked
                ],
            }
        else:
            header = selected["names"] if include_header else []
            lines = []
            if header:
                lines.append("| " + " | ".join(header) + " |")
                lines.append("| " + " | ".join(["---"] * len(header)) + " |")
            lines.extend(_md_row(values) for _, values in picked)
            body = {"markdown": "\n".join(lines)}
        next_cursor = last + 1 if has_more else None
        result = {
            **body,
            "document_ref": document_ref,
            "sheet": sheet_meta["name"],
            "rows_returned": [picked[0][0], last] if picked else [start, start - 1],
            "sheet_total_rows": total,
            "rows_scanned": len(picked),
            "quality": "partial" if invalid_cells else "available",
            "invalid_cell_count": invalid_cells,
            "all_columns": not invalid_cells and set(selected["indices"]) == set(range(len(sheet_meta["columns"]))),
            "has_more": has_more,
            "next_row_cursor": next_cursor,
            "signature": self._sign(sheet_meta["name"], picked[0][0] if picked else start, last),
        }

        if _response_bytes(result) > 32000:
            if len(picked) <= 1:
                raise StructuredStoreError("DOCUMENT_RESULT_TOO_LARGE", "Select fewer columns; a single row exceeds the response budget")
            reduced = self.read_range(document_ref, sheet=sheet, rows=[start, picked[len(picked) // 2 - 1][0]],
                                      columns=columns, format=format, include_header=include_header)
            last = reduced["rows_returned"][1]
            reduced["has_more"] = last < total and (end is None or last < end)
            reduced["next_row_cursor"] = last + 1 if reduced["has_more"] else None
            return reduced
        return result

    def read_cell(self, document_ref, *, sheet, row, column, offset=0):
        entry, manifest = self._entry(document_ref)
        meta = self._sheet(manifest, sheet)
        selected = self._columns(meta, [column])
        if row > meta["rows"]:
            raise StructuredStoreError("DOCUMENT_ARGUMENT_INVALID", "Cell row is out of range")
        _, values = next(self._iter_rows(entry, meta, row, row))
        value = values[selected["indices"][0]]
        text = "" if value is None else str(value)
        if offset > len(text):
            raise StructuredStoreError("DOCUMENT_ARGUMENT_INVALID", "Cell cursor is out of range")
        fragment = text[offset:offset + 4000]
        end = offset + len(fragment)
        return {"document_ref": document_ref, "content_mode": "cell", "sheet": meta["name"],
                "row": row, "column": column, "text": fragment, "offset": offset,
                "total_chars": len(text), "next_cell_cursor": end if end < len(text) else None,
                "cell_complete": offset == 0 and end == len(text), "all_columns": False,
                "signature": self._sign(meta["name"], row, row)}

    def aggregate(self, document_ref: str, ops: list[dict[str, Any]]) -> dict[str, Any]:
        entry, manifest = self._entry(document_ref)
        reason = invalid_ops(ops)
        if reason:
            raise StructuredStoreError("DOCUMENT_ARGUMENT_INVALID", reason)
        results = []
        size = 0
        truncated = False
        for op in ops:
            sheet_name = str(op.get("sheet") or "")
            union_key = op.get("cross_sheet_union") or {}
            if not isinstance(union_key, dict) or (union_key and (not isinstance(union_key.get("key_column"), str) or not union_key["key_column"])):
                raise StructuredStoreError("DOCUMENT_ARGUMENT_INVALID", "cross_sheet_union is invalid")
            key_column = union_key.get("key_column", "")
            if key_column and (op.get("row_range") is not None or sheet_name not in {"", "*"}):
                raise StructuredStoreError("DOCUMENT_ARGUMENT_INVALID", "union cannot also select sheet or row_range")
            if key_column:
                rows_iter, sheet_meta, scanned, sources = self._union_rows(entry, manifest, key_column, op)
                full_range = True
                matched_range: list[int] | None = None
            else:
                sheet_meta = self._sheet(manifest, sheet_name or None)
                row_range = op.get("row_range")
                start = 1
                end = sheet_meta["rows"]
                if row_range is not None:
                    _validate_range(row_range)
                    start = max(1, int(row_range[0]))
                    end = min(sheet_meta["rows"], int(row_range[1]))
                full_range = start <= 1 and end >= sheet_meta["rows"]
                matched_range = [start, end]
                rows_iter = self._iter_rows(entry, sheet_meta, start, end, aggregate=True)
                scanned = max(0, end - start + 1)
                sources = [{"sheet": sheet_meta["name"], "range": [start, end], "rows_scanned": scanned}]
            result = self._aggregate_op(sheet_meta, rows_iter, op, key_column, workspace=entry.path)
            result.update(
                sheet="*" if key_column else sheet_meta["name"],
                sources=sources,
                filter=op.get("filter") or None,
                rows_scanned=scanned,
                full_range=full_range,
                range=matched_range,
            )
            if result.get("groups_complete") is False:
                truncated = True
            encoded = _response_bytes(result)
            if size + encoded > min(self.page_chars, 26000):
                truncated = True
                break
            size += encoded
            results.append(result)
        return {
            "document_ref": document_ref,
            "results": results,
            "truncated": truncated,
            "signature": self._sign("aggregate", len(results), len(results)),
        }

    def search(
        self,
        document_ref: str,
        *,
        query: str,
        sheet: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        entry, manifest = self._entry(document_ref)
        needle = str(query or "").lower()
        if not needle:
            raise StructuredStoreError("DOCUMENT_ARGUMENT_INVALID", "search query is empty")
        limit = max(1, min(int(limit), 100))
        hits = []
        targets = (
            [self._sheet(manifest, sheet)]
            if sheet
            else [self._sheet(manifest, meta["name"]) for meta in manifest["inventory"]["sheets"]]
        )
        for sheet_meta in targets:
            for row_number, values in self._iter_rows(entry, sheet_meta, 1):
                if any(needle in str(value).lower() for value in values if value is not None):
                    hit = {"sheet": sheet_meta["name"], "row": row_number, "values": values}
                    if _response_bytes(hit) > 14000:
                        hit = {"sheet": sheet_meta["name"], "row": row_number,
                               "preview": " ".join(str(v)[:100] for v in values[:8])[:800],
                               "values_truncated": True, "read_hint": "Use read_range format=cell for full values"}
                    if hits and _response_bytes(hits + [hit]) > 24000:
                        return {"document_ref": document_ref, "hits": hits, "truncated": True,
                                "signature": self._sign("search", len(hits), len(hits))}
                    hits.append(hit)
                    if len(hits) >= limit:
                        return {
                            "document_ref": document_ref,
                            "hits": hits,
                            "truncated": True,
                            "signature": self._sign("search", len(hits), len(hits)),
                        }
        return {
            "document_ref": document_ref,
            "hits": hits,
            "truncated": False,
            "signature": self._sign("search", len(hits), len(hits)),
        }

    def read_chunks(self, document_ref: str, *, cursor: str | None, limit: int) -> dict[str, Any]:
        entry, manifest = self._entry(document_ref)
        if not 1 <= int(limit) <= 10:
            raise StructuredStoreError("FILE_REF_INVALID", "Chunk limit is invalid")
        inventory = manifest["inventory"]
        descriptors = []
        for meta in inventory["sheets"]:
            ranges = meta.get("legacy_blocks")
            if ranges is None:
                ranges = [[start, min(meta["rows"], start + CHUNK_SIM_ROWS - 1)]
                          for start in range(1, meta["rows"] + 1, CHUNK_SIM_ROWS)]
            descriptors.extend((meta, start, end) for start, end in ranges)
        offset = self._cursor_offset("cs1", cursor, document_ref) if cursor else 0
        if offset < 0 or offset > len(descriptors):
            raise StructuredStoreError("FILE_REF_INVALID", "Chunk cursor is out of range")
        chunks = []
        def response():
            next_offset = offset + len(chunks)
            more = next_offset < len(descriptors)
            return {
                "document_ref": document_ref, "chunks": list(chunks),
                "next_cursor": self._cursor_token("cs1", document_ref, next_offset) if more else None,
                "has_more": more, "coverage": {"read": next_offset, "total": len(descriptors)},
            }
        for index in range(offset, min(len(descriptors), offset + int(limit))):
            meta, start, end = descriptors[index]
            rows = list(self._iter_rows(entry, meta, start, end))
            chunks.append({"index": index, "heading": meta["name"],
                           "rows_returned": [start, end],
                           "text": render_markdown([c["name"] for c in meta["columns"]], rows)})
            if _response_bytes(response()) > 32000:
                chunks.pop()
                if not chunks:
                    raise StructuredStoreError("DOCUMENT_RESULT_TOO_LARGE", "Use read_range with fewer columns for this block")
                break
        return response()

    # ---------------------------------------------------------------- helpers

    def _aggregate_op(self, sheet_meta, rows_iter, op: dict[str, Any], key_column: str, *, workspace=None):
        filters = op.get("filter") or {}
        group_by = op.get("group_by") or []
        metrics = op.get("metrics")
        names = [column["name"] for column in sheet_meta["columns"]]
        index_of = {name: position for position, name in enumerate(names)}
        if not isinstance(group_by, list) or any(not isinstance(name, str) or name not in names for name in group_by) or len(set(group_by)) != len(group_by):
            raise StructuredStoreError("DOCUMENT_ARGUMENT_INVALID", "aggregate group_by is invalid")
        if not isinstance(metrics, list) or not metrics:
            raise StructuredStoreError("DOCUMENT_ARGUMENT_INVALID", "aggregate metrics are required")
        for metric in metrics:
            if not isinstance(metric, dict) or not isinstance(metric.get("fn"), str) or metric.get("fn") not in _METRICS or metric.get("column") not in names:
                reason = "METRIC_FUNCTION" if metric.get("fn") not in _METRICS else "METRIC_COLUMN"
                raise StructuredStoreError("DOCUMENT_ARGUMENT_INVALID", reason)
        if not isinstance(filters, dict):
            raise StructuredStoreError("DOCUMENT_ARGUMENT_INVALID", "aggregate filter is invalid")
        if filters:
            column = filters.get("column")
            op_name = filters.get("op")
            value = filters.get("value")
            if column not in names or not isinstance(op_name, str) or op_name not in _FILTER_OPS or (op_name == "in" and not isinstance(value, list)):
                raise StructuredStoreError("DOCUMENT_ARGUMENT_INVALID", "aggregate filter is invalid")
        cursor = op.get("group_cursor")
        if cursor is not None and (type(cursor) is not int or cursor < 0):
            raise StructuredStoreError("DOCUMENT_ARGUMENT_INVALID", "group_cursor must be nonnegative")
        try:
            return disk_aggregate(rows_iter, names=names, group_by=group_by, metrics=metrics,
                filters=filters, match=_match, directory=workspace or self.root, max_bytes=self.max_document_bytes,
                max_groups=self.max_groups, group_cursor=cursor)
        except SpreadsheetExtractError as exc:
            raise StructuredStoreError(exc.code, str(exc)) from exc
        except (OverflowError, sqlite3.OperationalError) as exc:
            raise StructuredStoreError("DOCUMENT_RESULT_TOO_LARGE", "Aggregate workspace or group page exceeds quota; use a smaller scope or group_cursor=0") from exc

    def _union_rows(self, entry: _Entry, manifest, key_column: str, op: dict):
        selected = []
        first_meta = None
        sources = []
        metrics = op.get("metrics")
        groups = op.get("group_by") or []
        if not isinstance(metrics, list) or not all(isinstance(m, dict) and isinstance(m.get("column"), str) for m in metrics) or not isinstance(groups, list) or not all(isinstance(g, str) for g in groups):
            raise StructuredStoreError("DOCUMENT_ARGUMENT_INVALID", "Union metrics or groups are invalid")
        required = {m["column"] for m in metrics}
        required.update(groups)
        if isinstance(op.get("filter"), dict) and op["filter"]:
            required.add(op["filter"].get("column"))
        scanned = 0
        for meta in manifest["inventory"]["sheets"]:
            sheet_meta = self._sheet(manifest, meta["name"])
            names = [column["name"] for column in sheet_meta["columns"]]
            if key_column not in names:
                continue
            if not required.issubset(names):
                raise StructuredStoreError("DOCUMENT_ARGUMENT_INVALID", "Union source lacks a required column")
            sources.append({"sheet": sheet_meta["name"], "range": [1, sheet_meta["rows"]], "rows_scanned": sheet_meta["rows"]})
            if first_meta is None:
                first_meta = sheet_meta
                base_names = names
            selected.append((sheet_meta, {name: position for position, name in enumerate(names)}))
            scanned += sheet_meta["rows"]
        if first_meta is None:
            raise StructuredStoreError("DOCUMENT_ARGUMENT_INVALID", "union key column is absent")
        def stream():
            for meta, lookup in selected:
                for number, values in self._iter_rows(entry, meta, 1, aggregate=True):
                    yield number, SourceRow([values[lookup[name]] if name in lookup and lookup[name] < len(values) else None for name in base_names], [i for i, name in enumerate(base_names) if lookup.get(name) in values.invalid_columns])
        return stream(), first_meta, scanned, sources

    def _entry(self, document_ref: str) -> tuple[_Entry, dict[str, Any]]:
        document_hash = self._token_hash("ds1", document_ref)
        entry = self._documents.get(document_hash)
        if entry is None or entry.expires_at <= _utc(self.clock()):
            self._documents.pop(document_hash, None)
            raise StructuredStoreError("DOCUMENT_REF_EXPIRED", "Document reference expired")
        manifest_path = entry.path / "manifest.json"
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            stored = payload.pop("sha256", "")
            digest = hashlib.sha256(
                json.dumps(payload, sort_keys=True).encode("utf-8")
            ).hexdigest()
        except (OSError, json.JSONDecodeError) as exc:
            raise StructuredStoreError(
                "DOCUMENT_REF_EXPIRED", "Document result is unavailable"
            ) from exc
        if stored != digest:
            raise StructuredStoreError("FILE_REF_INVALID", "Document result integrity failed")
        inventory = payload["inventory"]
        if inventory.get("engine") == "ooxml-1" and any(sheet.get("formula_cache_status") not in ({"available", "not_applicable", "partial"} if inventory.get("format_version") == 2 else {"available", "not_applicable"}) for sheet in inventory["sheets"]):
            raise StructuredStoreError("DOCUMENT_REF_EXPIRED", "Reparse workbook with formula cache validation")
        payload["sha256"] = stored
        return entry, payload

    def _sheet(self, manifest: dict[str, Any], name: str | None) -> dict[str, Any]:
        sheets = manifest["inventory"]["sheets"]
        if name is None:
            if len(sheets) != 1:
                raise StructuredStoreError(
                    "DOCUMENT_ARGUMENT_INVALID", "sheet parameter is required for multi-sheet workbooks"
                )
            return sheets[0]
        for sheet in sheets:
            if sheet["name"] == name:
                return sheet
        raise StructuredStoreError("DOCUMENT_ARGUMENT_INVALID", "sheet is not in this workbook")

    def _columns(self, sheet_meta: dict[str, Any], columns: list[str] | None):
        names = [column["name"] for column in sheet_meta["columns"]]
        if not columns:
            return {"names": names, "indices": list(range(len(names)))}
        if not isinstance(columns, list) or any(not isinstance(name, str) or name not in names for name in columns) or len(set(columns)) != len(columns):
            raise StructuredStoreError("DOCUMENT_ARGUMENT_INVALID", "columns must be unique existing names")
        return {"names": list(columns), "indices": [names.index(name) for name in columns]}

    def _iter_rows(
        self,
        entry: _Entry,
        sheet_meta: dict[str, Any],
        start: int,
        end: int | None = None,
        *, aggregate: bool = False,
    ):
        path = entry.path / sheet_meta["file"]
        if not path.is_file():
            raise StructuredStoreError("DOCUMENT_REF_EXPIRED", "Sheet data is unavailable")
        offsets = sheet_meta["block_offsets"]
        block = max(0, (start - 1) // sheet_meta["block_rows"])
        with path.open("rb") as handle:
            handle.seek(offsets[min(block, len(offsets) - 1)])
            for line in handle:
                if not line.strip():
                    continue
                record = json.loads(line)
                number = int(record["r"])
                if number < start:
                    continue
                if end is not None and number > end:
                    return
                yield number, SourceRow(record.get("a", record["v"]) if aggregate else record["v"], record.get("e", []))

    def _chunk_count(self, inventory: dict[str, Any]) -> int:
        return sum(
            len(sheet["legacy_blocks"]) if "legacy_blocks" in sheet else
            (sheet["rows"] + CHUNK_SIM_ROWS - 1) // CHUNK_SIM_ROWS
            for sheet in inventory["sheets"]
        )

    # ---------------------------------------------------------------- tokens

    def _token(self, prefix: str, nonce: bytes) -> str:
        mac = hmac.new(self.key, prefix.encode() + b"\0" + nonce, hashlib.sha256).digest()
        return f"{prefix}_{nonce.hex()}_{mac.hex()}"

    def _token_hash(self, prefix: str, token: str) -> str:
        parts = str(token or "").split("_")
        if len(parts) != 3 or parts[0] != prefix:
            raise StructuredStoreError("FILE_REF_INVALID", "Document reference is invalid")
        try:
            nonce = bytes.fromhex(parts[1])
            supplied = bytes.fromhex(parts[2])
        except ValueError as exc:
            raise StructuredStoreError(
                "FILE_REF_INVALID", "Document reference is invalid"
            ) from exc
        expected = hmac.new(self.key, prefix.encode() + b"\0" + nonce, hashlib.sha256).digest()
        if len(nonce) != 32 or not hmac.compare_digest(supplied, expected):
            raise StructuredStoreError("FILE_REF_INVALID", "Document reference is invalid")
        return hashlib.sha256(nonce).hexdigest()

    def _cursor_token(self, prefix: str, document_ref: str, offset: int) -> str:
        message = f"{prefix}\0{document_ref}\0{offset}".encode()
        nonce = hmac.new(self.key, message, hashlib.sha256).digest()[:16]
        mac = hmac.new(self.key, message + nonce, hashlib.sha256).digest()
        return f"{prefix}_{offset}_{nonce.hex()}_{mac.hex()}"

    def _cursor_offset(self, prefix: str, cursor: str, document_ref: str) -> int:
        parts = str(cursor or "").split("_")
        if len(parts) != 4 or parts[0] != prefix:
            raise StructuredStoreError("FILE_REF_INVALID", "Document cursor is invalid")
        try:
            offset = int(parts[1])
            nonce = bytes.fromhex(parts[2])
            supplied = bytes.fromhex(parts[3])
        except ValueError as exc:
            raise StructuredStoreError(
                "FILE_REF_INVALID", "Document cursor is invalid"
            ) from exc
        message = f"{prefix}\0{document_ref}\0{offset}".encode()
        expected = hmac.new(self.key, message + nonce, hashlib.sha256).digest()
        if not hmac.compare_digest(supplied, expected):
            raise StructuredStoreError("FILE_REF_INVALID", "Document cursor is invalid")
        return offset

    def _sign(self, scope: str, start: int, end: int) -> str:
        message = f"echo\0{scope}\0{start}\0{end}".encode()
        return hmac.new(self.key, message, hashlib.sha256).hexdigest()[:32]

    # ---------------------------------------------------------------- cleanup

    def delete_task(self, task_id: str) -> None:
        normalized = str(task_id or "").strip()
        for document_hash, entry in list(self._documents.items()):
            if entry.task_id == normalized:
                self._documents.pop(document_hash, None)
        task_root = (self.root / normalized).resolve(strict=False)
        derived = task_root / _DIR_NAME
        if task_root.parent == self.root and derived.exists() and not derived.is_symlink():
            shutil.rmtree(derived)

    def purge_expired(self) -> int:
        now = _utc(self.clock())
        expired = [key for key, entry in self._documents.items() if entry.expires_at <= now]
        for key in expired:
            entry = self._documents.pop(key)
            shutil.rmtree(entry.path, ignore_errors=True)
        return len(expired)

    def clear_all(self) -> None:
        self._documents.clear()
        if not self.root.is_dir() or self.root.is_symlink():
            return
        for task_root in self.root.iterdir():
            derived = task_root / _DIR_NAME
            if task_root.is_dir() and derived.is_dir() and not derived.is_symlink():
                shutil.rmtree(derived)


def _load_key(path: Path) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        os.chmod(path, 0o600)
        token = path.read_bytes()
        if len(token) >= 32:
            return token
        path.unlink()
    token = secrets.token_bytes(32)
    with path.open("xb") as handle:
        os.chmod(path, 0o600)
        handle.write(token)
    return token


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(timezone.utc)


def _md_row(values: Iterable[Any]) -> str:
    return "| " + " | ".join("" if value is None else str(value) for value in values) + " |"


def _match(value: Any, op: str, expected: Any) -> bool:
    if op == "eq":
        return value == expected
    if op == "ne":
        return value != expected
    if op in {"gt", "gte", "lt", "lte"}:
        try:
            left, right = float(value), float(expected)
        except (TypeError, ValueError):
            left, right = str(value), str(expected)
        return {
            "gt": left > right,
            "gte": left >= right,
            "lt": left < right,
            "lte": left <= right,
        }[op]
    if op == "contains":
        return str(expected).lower() in str(value or "").lower()
    if op == "in":
        return isinstance(expected, list) and value in expected
    return False


def _metric(fn: str | None, values: list[Any]) -> Any:
    present = [value for value in values if value is not None and value != ""]
    if fn == "count":
        return len(values)
    if fn == "count_distinct":
        return len({json.dumps(value, ensure_ascii=False, sort_keys=True) for value in present})
    if not present:
        return None
    if fn in {"sum", "avg", "min", "max", "median"}:
        numbers = []
        for value in present:
            try:
                numbers.append(float(value))
            except (TypeError, ValueError):
                return None
        if fn == "sum":
            return round(sum(numbers), 6)
        if fn == "avg":
            return round(sum(numbers) / len(numbers), 6)
        if fn == "min":
            return min(numbers)
        if fn == "max":
            return max(numbers)
        return round(median(numbers), 6)
    return None


__all__ = ["StructuredStore", "StructuredStoreError"]


def _response_bytes(value):
    return len(json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8"))


def _validate_range(rows):
    if not isinstance(rows, (list, tuple)) or len(rows) != 2 or any(type(value) is not int for value in rows) or rows[0] < 1 or rows[1] < rows[0]:
        raise StructuredStoreError("DOCUMENT_ARGUMENT_INVALID", "row range must be ordered positive integers")
