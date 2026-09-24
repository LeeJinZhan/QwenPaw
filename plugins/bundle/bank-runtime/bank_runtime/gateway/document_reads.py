"""Request-local read evidence from admitted tool results, never model text."""
from collections.abc import Mapping
from dataclasses import dataclass, field
import hashlib
import json
import re

from .completion import _result_values

READ_ERROR_CODES = frozenset({
    "DOCUMENT_ARGUMENT_INVALID", "DOCUMENT_FORMULA_CACHE_MISSING",
    "DOCUMENT_READ_INCOMPLETE", "DOCUMENT_READ_NO_PROGRESS", "DOCUMENT_PARSE_FAILED",
    "DOCUMENT_REF_EXPIRED", "DOCUMENT_RESULT_TOO_LARGE", "DOCUMENT_TEXT_TRUNCATED",
    "DOCUMENT_TEXT_ENCODING_UNSUPPORTED", "MINERU_TIMEOUT", "MINERU_UNAVAILABLE", "MINERU_SUBMIT_AMBIGUOUS",
})
FILE_POLICY_CODES = frozenset({"FILE_ACCESS_DENIED", "FILE_REF_INVALID", "FILE_REF_EXPIRED", "FILE_TYPE_UNSUPPORTED"})


def is_read_recovery_tool(name):
    return name.endswith((
        "parse_documents", "read_document_chunks", "read_range", "aggregate", "search",
    )) or name in {
        "Skill", "artifact_convert", "runtime_sandbox_files_search", "runtime_sandbox_files_select",
    }


FULL_CLAIM_KEYWORDS = (
    "全量", "全表", "全部材料", "所有材料", "全部文件", "所有文件", "全部sheet", "全部 sheet", "所有sheet", "所有 sheet",
    "所有工作表", "合计", "总计", "读完", "完整读取", "全部内容", "全文", "完整分析",
    "平均", "中位数", "计数", "去重数量", "最小", "最大",
)
ALL_SHEET_KEYWORDS = ("全量", "全表", "全部", "所有", "所有工作表")


def result_objects(content):
    values = []
    for block in content or []:
        kind = block.get("type") if isinstance(block, Mapping) else getattr(block, "type", "")
        text = block.get("text", "") if isinstance(block, Mapping) else getattr(block, "text", "")
        if kind != "text":
            continue
        decoded = _result_values(text)
        if not decoded:
            return []
        values.extend(value for value in decoded if isinstance(value, Mapping))
    return values


def result_error(content):
    errors = []
    for value in result_objects(content):
        items = value.get("items")
        candidates = [*items, value] if isinstance(items, list) else [value]
        for item in candidates:
            if isinstance(item, Mapping) and item.get("status") == "failed":
                code = item.get("error_code")
                errors.append(code if isinstance(code, str) and code in READ_ERROR_CODES | FILE_POLICY_CODES else "DOCUMENT_PARSE_FAILED")
    return next((code for code in errors if code in FILE_POLICY_CODES), "") or (
        "MINERU_SUBMIT_AMBIGUOUS" if "MINERU_SUBMIT_AMBIGUOUS" in errors else next(iter(errors), ""))


@dataclass
class _Read:
    file_id: str
    total: int
    chunks: dict[int, str] = field(default_factory=dict)
    cursors: dict[str | None, int] = field(default_factory=lambda: {None: 0})
    terminal: bool = False
    no_progress: int = 0
    last_range_complete: bool = False
    error: str = ""
    inventory: dict[str, int] = field(default_factory=dict)
    inventory_complete: bool = True
    column_names: set[str] = field(default_factory=set)
    covered: dict[str, list] = field(default_factory=dict)
    aggregates: list[dict] = field(default_factory=list)
    aggregate_pages: dict[str, list[list[int]]] = field(default_factory=dict)
    aggregate_stalls: dict[str, int] = field(default_factory=dict)
    repeated_statistics: bool = False
    touched: set = field(default_factory=set)
    cell_fragments: set[str] = field(default_factory=set)
    projections: set[str] = field(default_factory=set)

    @property
    def structured(self):
        return bool(self.inventory)

    @property
    def complete(self):
        if self.error:
            return False
        if self.structured:
            return self.inventory_complete and all(self.sheet_full(name) for name in self.inventory)
        return self.terminal and len(self.chunks) == self.total

    def sheet_full(self, name: str) -> bool:
        total = self.inventory.get(name, 0)
        if total <= 0:
            return True
        return self.covered_rows(name) >= total

    def covered_rows(self, name: str) -> int:
        merged: list[list[int]] = []
        for start, end in sorted(self.covered.get(name, [])):
            if merged and start <= merged[-1][1] + 1:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        return sum(end - start + 1 for start, end in merged if end >= start)

    def add_range(self, name: str, start: int, end: int) -> bool:
        if end < start:
            return False
        before = self.covered_rows(name)
        self.covered.setdefault(name, []).append([start, end])
        return self.covered_rows(name) > before


class DocumentReadLedger:
    def __init__(self):
        self.documents: dict[str, _Read] = {}
        self.source_refs: dict[str, str] = {}
        self.failures: dict[str, str] = {}
        self.attempts: dict[str, int] = {}
        self.argument_failures: dict[str, int] = {}

    @property
    def pending(self):
        # Completed statistics never grant an exemption to unfinished reads
        # or other aggregate operations. Their progress budgets are separate.
        return bool(self.failures) or any(
            (not doc.complete and (not doc.structured or (doc.no_progress >= 3 and not doc.last_range_complete)))
            or bool(doc.aggregate_stalls)
            for doc in self.documents.values()
        )

    @property
    def argument_retry_exhausted(self):
        return any(self.argument_failures.get(key, 0) >= 2
                   for key, code in self.failures.items() if code == "DOCUMENT_ARGUMENT_INVALID")

    @property
    def error_code(self):
        if any(code in FILE_POLICY_CODES for code in self.failures.values()):
            # Preserve the existing denial/unknown-result presentation and do
            # not turn a scope rejection into a retryable read failure.
            return "ARTIFACT_OUTPUT_MISSING"
        if "MINERU_SUBMIT_AMBIGUOUS" in self.failures.values():
            return "MINERU_SUBMIT_AMBIGUOUS"
        if "DOCUMENT_ARGUMENT_INVALID" in self.failures.values():
            return "DOCUMENT_ARGUMENT_INVALID"
        if any(self.attempts.get(key, 0) >= 3 for key in self.failures):
            return "DOCUMENT_READ_NO_PROGRESS"
        for doc in self.documents.values():
            if ((doc.no_progress >= 3 and not doc.last_range_complete and not doc.complete)
                    or any(count >= 3 for count in doc.aggregate_stalls.values())):
                return "DOCUMENT_READ_NO_PROGRESS"
        return next(iter(self.failures.values()), "") or next(
            (doc.error for doc in self.documents.values() if doc.error), "DOCUMENT_READ_INCOMPLETE")

    def coverage(self, ref):
        doc = self.documents[ref]
        return len(doc.chunks), doc.total

    @staticmethod
    def _aggregate_scope(op):
        if not isinstance(op, Mapping):
            return op
        # Absent/default options describe the same computation on continuation.
        return {k: v for k, v in op.items() if k != "group_cursor"
                and not (k in {"sheet", "row_range", "filter", "cross_sheet_union"} and v is None)
                and not (k == "group_by" and v in (None, []))}

    @staticmethod
    def _aggregate_keys(payload):
        ref = str(payload.get("document_ref") or "")
        ops = payload.get("ops")
        if not isinstance(ops, list) or not ops:
            ops = [ops]
        keys = set()
        for op in ops:
            # Cursor advancement recovers the same operation; changing its
            # sheet, range, grouping, filter or metrics does not.
            scope = DocumentReadLedger._aggregate_scope(op)
            digest = hashlib.sha256(json.dumps(scope, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            keys.add(f"aggregate:{ref}:{digest}")
        return keys

    def _fail_aggregate(self, payload, code):
        keys = self._aggregate_keys(payload)
        if code == "DOCUMENT_ARGUMENT_INVALID":
            # Schema rejection did not execute an operation. Keep the existing
            # bounded parameter correction, without erasing prior real errors.
            for key in keys:
                if self.failures.get(key) == "DOCUMENT_READ_INCOMPLETE" and self.attempts.get(key) == 1:
                    self.failures.pop(key, None)
                    self.attempts.pop(key, None)
            self.failures["aggregate-argument:" + str(payload.get("document_ref") or "")] = code
            return
        for key in keys:
            self.failures[key] = code

    def _clear_aggregate_reference(self, ref):
        for collection in (self.failures, self.attempts, self.argument_failures):
            for key in list(collection):
                if key.startswith(f"aggregate:{ref}:") or key == "aggregate-argument:" + ref:
                    collection.pop(key, None)

    @property
    def successful_read_repeats(self):
        """Stop a redundant loop independently of delivery completeness."""
        return not self.pending and not any(doc.error for doc in self.documents.values()) and any(
            doc.structured and doc.no_progress >= 3 and (doc.last_range_complete or doc.complete)
            for doc in self.documents.values())

    def start(self, name, payload):
        if name.endswith("parse_documents"):
            for item in payload.get("documents", []):
                if isinstance(item, Mapping):
                    key = "parse:" + str(item.get("file_id") or "")
                    self.failures[key] = "DOCUMENT_PARSE_FAILED"
                    self.attempts[key] = self.attempts.get(key, 0) + 1
        elif name.endswith(("read_document_chunks", "read_range", "aggregate")):
            keys = self._aggregate_keys(payload) if name.endswith("aggregate") else ["read:" + str(payload.get("document_ref") or "")]
            for key in keys:
                self.failures.setdefault(key, "DOCUMENT_READ_INCOMPLETE")
                self.attempts[key] = self.attempts.get(key, 0) + 1

    def observe(self, name, payload, content, success):
        values = result_objects(content)
        code = result_error(content)
        if code == "DOCUMENT_ARGUMENT_INVALID" and name.endswith(("parse_documents", "read_range", "aggregate", "read_document_chunks")):
            keys = ["parse:" + str(item.get("file_id") or "") for item in payload.get("documents", []) if isinstance(item, Mapping)] if name.endswith("parse_documents") else [("aggregate-argument:" if name.endswith("aggregate") else "read:") + str(payload.get("document_ref") or "")]
            for key in keys:
                self.argument_failures[key] = self.argument_failures.get(key, 0) + 1
        if name.endswith("read_range"):
            return self._observe_read_range(name, payload, values, code, success)
        if name.endswith("aggregate"):
            return self._observe_aggregate(name, payload, values, code, success)
        if name.endswith("search"):
            return self._observe_search(payload, values, success)
        if name.endswith("parse_documents"):
            requested = {str(item.get("file_id")) for item in payload.get("documents", []) if isinstance(item, Mapping)}
            grouped = {}
            for value in values:
                for item in value.get("items", []) if isinstance(value.get("items"), list) else []:
                    if isinstance(item, Mapping) and isinstance(item.get("file_id"), str) and item["file_id"] in requested:
                        grouped.setdefault(item["file_id"], []).append(item)
            for file_id in requested:
                items = grouped.get(file_id, [])
                if not success or not items or any(value.get("status") == "failed" for value in values) or any(item.get("status") != "completed" for item in items):
                    self.failures["parse:" + file_id] = code or "DOCUMENT_PARSE_FAILED"
                    continue
                # Conflicting duplicate metadata is not proof of a complete parse.
                metadata = [(item.get("content_mode"), item.get("document_ref"), item.get("chunk_count")) for item in items]
                if any(any(value is not None and not isinstance(value, (str, int)) for value in row) for row in metadata):
                    continue
                signatures = set(metadata)
                if len(signatures) != 1:
                    continue
                item = items[0]
                mode, ref, count = next(iter(signatures))
                if isinstance(ref, str) and ref:
                    sources = {entry.get("file_ref") for entry in payload.get("documents", [])
                               if isinstance(entry, Mapping) and entry.get("file_id") == file_id
                               and isinstance(entry.get("file_ref"), str)}
                    if len(sources) == 1:
                        self.source_refs[ref] = next(iter(sources))
                if mode not in (None, "inline", "chunked", "structured"):
                    continue
                if mode == "structured":
                    raw_inventory = item.get("inventory") if isinstance(item.get("inventory"), Mapping) else {}
                    inventory = {
                        str(sheet.get("name")): int(sheet.get("rows"))
                        for sheet in (raw_inventory.get("sheets") or [])
                        if isinstance(sheet, Mapping) and isinstance(sheet.get("name"), str)
                        and type(sheet.get("rows")) is int and sheet.get("rows") >= 0
                    }
                    if not inventory or not isinstance(ref, str) or not ref:
                        continue
                    for old_ref, doc in list(self.documents.items()):
                        if doc.file_id == file_id and old_ref != ref:
                            del self.documents[old_ref]
                            self.failures.pop("read:" + old_ref, None)
                            self._clear_aggregate_reference(old_ref)
                    existing = self.documents.get(ref)
                    if existing:
                        if existing.file_id != file_id or existing.inventory != inventory:
                            existing.error = "DOCUMENT_READ_INCOMPLETE"
                            continue
                        self.failures.pop("parse:" + file_id, None)
                        self.attempts.pop("parse:" + file_id, None)
                        continue
                    self.documents[ref] = _Read(
                        file_id, sum(inventory.values()), inventory=inventory,
                        inventory_complete=raw_inventory.get("inventory_complete", True) is True,
                        column_names={column["name"] for sheet in raw_inventory.get("sheets", [])
                                      if isinstance(sheet, Mapping) for column in sheet.get("columns", [])
                                      if isinstance(column, Mapping) and isinstance(column.get("name"), str)}
                    )
                    self.failures.pop("parse:" + file_id, None)
                    self.attempts.pop("parse:" + file_id, None)
                    continue
                if mode == "inline" and (not isinstance(item.get("markdown"), str) or not item["markdown"]
                        or any(other.get("markdown") != item["markdown"] for other in items)):
                    continue
                if mode == "chunked" and (not isinstance(ref, str) or not ref or type(count) is not int or not 0 < count <= 200_000):
                    continue
                self.failures.pop("parse:" + file_id, None)
                self.attempts.pop("parse:" + file_id, None)
                for old_ref, doc in list(self.documents.items()):
                    if doc.file_id == file_id and old_ref != ref:
                        del self.documents[old_ref]
                        self.failures.pop("read:" + old_ref, None)
                        self._clear_aggregate_reference(old_ref)
                if mode == "inline" and isinstance(ref, str) and ref:
                    self.documents[ref] = _Read(file_id, 0, terminal=True)
                if mode == "chunked":
                    existing = self.documents.get(ref)
                    if existing and (existing.file_id != file_id or existing.total != count):
                        self.failures["parse:" + file_id] = "DOCUMENT_READ_INCOMPLETE"
                    elif not existing:
                        self.documents[ref] = _Read(file_id, count)
            return
        if not name.endswith("read_document_chunks"):
            return
        ref = str(payload.get("document_ref") or "")
        key = "read:" + ref
        doc = self.documents.get(ref)
        if not success or code or not values or doc is None:
            self.failures[key] = code or "DOCUMENT_READ_INCOMPLETE"
            if doc:
                doc.no_progress += 1
            return
        if doc.structured:
            return self._observe_structured_chunks(payload, values, doc, key)
        # content + structuredContent may duplicate an identical page.
        if any(value != values[0] for value in values):
            doc.no_progress += 1
            return
        value = values[0]
        chunks = value.get("chunks")
        cursor = payload.get("cursor") or None
        offset = doc.cursors.get(cursor)
        more = value.get("has_more")
        next_cursor = value.get("next_cursor")
        if (value.get("document_ref") != ref or offset is None or not isinstance(chunks, list)
                or not 1 <= len(chunks) <= 10 or type(more) is not bool):
            doc.no_progress += 1
            return
        indices = [chunk.get("index") if isinstance(chunk, Mapping) else None for chunk in chunks]
        if (any(type(index) is not int for index in indices)
                or indices != list(range(offset, offset + len(chunks)))
                or offset + len(chunks) > doc.total
                or any(not isinstance(chunk.get("text"), str) for chunk in chunks)
                or more != (offset + len(chunks) < doc.total)
                or (more and (not isinstance(next_cursor, str) or not next_cursor or next_cursor == cursor))
                or (not more and next_cursor is not None)):
            doc.no_progress += 1
            return
        hashes = {chunk["index"]: hashlib.sha256(json.dumps(dict(chunk), sort_keys=True).encode()).hexdigest() for chunk in chunks}
        if any(index in doc.chunks and doc.chunks[index] != digest for index, digest in hashes.items()):
            doc.error = "DOCUMENT_READ_INCOMPLETE"
            doc.no_progress += 1
            return
        if more and next_cursor in doc.cursors and doc.cursors[next_cursor] != offset + len(chunks):
            doc.error = "DOCUMENT_READ_INCOMPLETE"
            return
        advanced = bool(set(hashes) - doc.chunks.keys())
        doc.chunks.update(hashes)
        doc.no_progress = 0 if advanced else doc.no_progress + 1
        doc.error = ""
        if more:
            doc.cursors[next_cursor] = offset + len(chunks)
        else:
            doc.terminal = True
        self.failures.pop(key, None)
        self.attempts.pop(key, None)
        self.argument_failures.pop(key, None)
        return

    def _observe_structured_chunks(self, payload, values, doc, key):
        doc.last_range_complete = False
        value = values[0]
        chunks = value.get("chunks")
        offset = doc.cursors.get(payload.get("cursor") or None)
        more = value.get("has_more")
        next_cursor = value.get("next_cursor")
        if (any(v != value for v in values) or value.get("document_ref") != payload.get("document_ref")
                or offset is None or not isinstance(chunks, list) or not 1 <= len(chunks) <= 10
                or type(more) is not bool
                or (more and (not isinstance(next_cursor, str) or not next_cursor or next_cursor == payload.get("cursor")))
                or (not more and next_cursor is not None)):
            doc.no_progress += 1
            return
        evidence = []
        for index, chunk in enumerate(chunks, offset):
            if not isinstance(chunk, Mapping):
                return
            sheet = chunk.get("heading")
            rows = chunk.get("rows_returned")
            if (chunk.get("index") != index or not isinstance(chunk.get("text"), str)
                    or sheet not in doc.inventory or not isinstance(rows, list) or len(rows) != 2
                    or any(type(n) is not int for n in rows)
                    or not 1 <= rows[0] <= rows[1] <= doc.inventory[sheet]):
                doc.no_progress += 1
                return
            digest = hashlib.sha256(json.dumps(dict(chunk), sort_keys=True).encode()).hexdigest()
            if index in doc.chunks and doc.chunks[index] != digest:
                doc.error = "DOCUMENT_READ_INCOMPLETE"
                return
            evidence.append((index, digest, sheet, rows))
        if more and next_cursor in doc.cursors and doc.cursors[next_cursor] != offset + len(chunks):
            return
        advanced = False
        for index, digest, sheet, rows in evidence:
            advanced = index not in doc.chunks or advanced
            doc.chunks[index] = digest
            advanced = doc.add_range(sheet, *rows) or advanced
            doc.touched.add(sheet)
        if more:
            doc.cursors[next_cursor] = offset + len(chunks)
        doc.no_progress = 0 if advanced else doc.no_progress + 1
        doc.error = ""
        self.failures.pop(key, None)
        self.attempts.pop(key, None)
        self.argument_failures.pop(key, None)

    def _observe_read_range(self, name, payload, values, code, success):
        ref = str(payload.get("document_ref") or "")
        key = "read:" + ref
        doc = self.documents.get(ref)
        if doc:
            doc.last_range_complete = False
        if not success or code or not values or doc is None or not doc.structured:
            self.failures[key] = code or "DOCUMENT_READ_INCOMPLETE"
            if doc:
                doc.no_progress += 1
            return
        value = values[0]
        if any(v != value for v in values) or value.get("document_ref") != ref:
            self.failures[key] = "DOCUMENT_READ_INCOMPLETE"
            return
        if payload.get("format") == "cell" and value.get("content_mode") == "cell":
            if value.get("sheet") in doc.inventory and isinstance(value.get("text"), str):
                signature = hashlib.sha256(json.dumps([payload, value], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                advanced = signature not in doc.cell_fragments
                doc.cell_fragments.add(signature)
                doc.touched.add(value["sheet"])
                doc.no_progress = 0 if advanced else doc.no_progress + 1
                self.failures.pop(key, None)
                self.attempts.pop(key, None)
                self.argument_failures.pop(key, None)
            return  # A cell fragment does not prove whole-row coverage.
        if payload.get("format") == "inventory" and value.get("content_mode") == "inventory":
            before = (dict(doc.inventory), set(doc.column_names), doc.inventory_complete)
            inventory = value.get("inventory")
            if isinstance(inventory, Mapping):
                for meta in inventory.get("sheets", []):
                    if isinstance(meta, Mapping) and isinstance(meta.get("name"), str) and type(meta.get("rows")) is int:
                        if meta["name"] in doc.inventory and doc.inventory[meta["name"]] != meta["rows"]:
                            doc.error = "DOCUMENT_READ_INCOMPLETE"
                            self.failures[key] = doc.error
                            return
                        doc.inventory[meta["name"]] = meta["rows"]
                        doc.column_names.update(c["name"] for c in meta.get("columns", []) if isinstance(c, Mapping) and isinstance(c.get("name"), str))
                # All sheet names must be known, including earlier pages.
                doc.inventory_complete = len(doc.inventory) == inventory.get("sheet_count")
            for meta in value.get("metadata", []):
                if isinstance(meta, Mapping) and meta.get("kind") == "column" and isinstance(meta.get("name"), str):
                    doc.column_names.add(meta["name"])
            advanced = before != (doc.inventory, doc.column_names, doc.inventory_complete)
            doc.no_progress = 0 if advanced else doc.no_progress + 1
            self.failures.pop(key, None)
            self.attempts.pop(key, None)
            self.argument_failures.pop(key, None)
            return
        sheet = value.get("sheet")
        echoed = value.get("rows_returned")
        total = value.get("sheet_total_rows")
        if (sheet not in doc.inventory or not isinstance(echoed, list) or len(echoed) != 2
                or total != doc.inventory.get(sheet) or not isinstance(value.get("signature"), str)):
            doc.no_progress += 1
            self.failures[key] = "DOCUMENT_READ_INCOMPLETE"
            return
        start, end = int(echoed[0]), int(echoed[1])
        if start < 1 or end > total or end < start - 1:
            doc.no_progress += 1
            self.failures[key] = "DOCUMENT_READ_INCOMPLETE"
            return
        advanced = sheet not in doc.touched
        if value.get("all_columns", True):
            advanced = doc.add_range(sheet, start, end) or advanced
        elif isinstance(payload.get("columns"), list):
            projection = json.dumps([sheet, start, end, sorted(payload["columns"])], ensure_ascii=False)
            advanced = projection not in doc.projections or advanced
            doc.projections.add(projection)
        doc.touched.add(sheet)
        requested = payload.get('rows')
        doc.last_range_complete = (
            isinstance(requested, list) and len(requested) == 2
            and all(type(row) is int for row in requested)
            and requested == [start, end]
            and payload.get('row_cursor') in (None, start)
            and value.get('all_columns', True) is True)
        doc.no_progress = 0 if advanced else doc.no_progress + 1
        doc.error = ""
        self.failures.pop(key, None)
        self.attempts.pop(key, None)
        self.argument_failures.pop(key, None)

    def _observe_aggregate(self, name, payload, values, code, success):
        ref = str(payload.get("document_ref") or "")
        doc = self.documents.get(ref)
        if not success or code or not values or doc is None or not doc.structured:
            self._fail_aggregate(payload, code or "DOCUMENT_READ_INCOMPLETE")
            return
        value = values[0]
        if (any(v != value for v in values) or value.get("document_ref") != ref
                or type(value.get("truncated")) is not bool):
            self._fail_aggregate(payload, "DOCUMENT_READ_INCOMPLETE")
            return
        results = value.get("results")
        if not isinstance(results, list) or not results:
            self._fail_aggregate(payload, "DOCUMENT_READ_INCOMPLETE")
            return
        ops = payload.get("ops")
        if not isinstance(ops, list) or len(ops) != len(results):
            self._fail_aggregate(payload, "DOCUMENT_READ_INCOMPLETE")
            return
        if value['truncated'] and not any(isinstance(result, Mapping) and result.get('groups_complete') is False
                                          and 'next_group_cursor' in result for op, result in zip(ops, results) if isinstance(op, Mapping)):
            self._fail_aggregate(payload, 'DOCUMENT_READ_INCOMPLETE')
            return
        evidence = []
        pages = []
        for op, result in zip(ops, results):
            sources = result.get("sources") if isinstance(result, Mapping) else None
            if (not isinstance(op, Mapping) or not isinstance(sources, list) or not sources
                    or result.get("metrics") != op.get("metrics")
                    or (result.get("group_by") or []) != (op.get("group_by") or [])
                    or result.get("filter") != (op.get("filter") or None)):
                self._fail_aggregate(payload, "DOCUMENT_READ_INCOMPLETE")
                return
            names = set()
            for source in sources:
                name = source.get("sheet") if isinstance(source, Mapping) else None
                rng = source.get("range") if isinstance(source, Mapping) else None
                total = doc.inventory.get(name)
                if (total is None or name in names or not isinstance(rng, list) or len(rng) != 2
                        or any(type(v) is not int for v in rng)
                        or rng[0] < 1 or rng[1] > total or rng[1] < rng[0] - 1
                        or source.get("rows_scanned") != max(0, rng[1] - rng[0] + 1)):
                    self._fail_aggregate(payload, "DOCUMENT_READ_INCOMPLETE")
                    return
                names.add(name)
            if (not op.get("cross_sheet_union") and
                    (len(names) != 1 or (op.get("sheet") and names != {op["sheet"]}))):
                self._fail_aggregate(payload, "DOCUMENT_READ_INCOMPLETE")
                return
            if 'group_cursor' in op or 'groups_complete' in result:
                offset, total, groups = op.get('group_cursor', 0), result.get('group_count'), result.get('groups')
                if (type(offset) is not int or type(total) is not int or offset < 0 or total < 0
                        or not isinstance(groups, list) or offset + len(groups) > total
                        or (not groups and (total != 0 or offset != 0))
                        or result.get('next_group_cursor') != (offset + len(groups) if offset + len(groups) < total else None)
                        or result.get('groups_complete') is not (offset == 0 and len(groups) == total)):
                    self._fail_aggregate(payload, 'DOCUMENT_READ_INCOMPLETE')
                    return
                identity = json.dumps({'op': self._aggregate_scope(op),
                                       'sources': sources, 'total': total}, sort_keys=True, ensure_ascii=False)
                pages.append((identity, offset, offset + len(groups), total, dict(result)))
            else:
                evidence.append(dict(result))
        for identity, start, end, total, result in pages:
            before = doc.aggregate_pages.get(identity, [])
            intervals = sorted([*doc.aggregate_pages.get(identity, []), [start, end]])
            merged = []
            for lower, upper in intervals:
                if merged and lower <= merged[-1][1]:
                    merged[-1][1] = max(merged[-1][1], upper)
                else:
                    merged.append([lower, upper])
            doc.aggregate_pages[identity] = merged
            doc.touched.update(source['sheet'] for source in result['sources'])
            if merged == [[0, total]]:
                doc.aggregate_stalls.pop(identity, None)
                # All result pages were observed; this still proves statistics,
                # never raw row/text coverage. Do not retain every group's data.
                result.update(groups=[], groups_complete=True, next_group_cursor=None)
                evidence.append(result)
            else:
                doc.aggregate_stalls[identity] = (
                    0 if merged != before else doc.aggregate_stalls.get(identity, 0) + 1)
        for item in evidence:
            if item not in doc.aggregates:
                doc.aggregates.append(item)
            else:
                doc.repeated_statistics = True
            doc.touched.update(source["sheet"] for source in item["sources"])
        # Do not clear raw-read errors, coverage or stall counters here.
        for key in [*self._aggregate_keys(payload), "aggregate-argument:" + ref]:
            self.failures.pop(key, None)
            self.attempts.pop(key, None)
            self.argument_failures.pop(key, None)

    def _observe_search(self, payload, values, success):
        ref = str(payload.get("document_ref") or "")
        doc = self.documents.get(ref)
        if doc is None or not doc.structured or not success or not values:
            return
        for hit in (values[0].get("hits") or [])[:100]:
            if isinstance(hit, Mapping) and hit.get("sheet") in doc.inventory:
                doc.touched.add(hit["sheet"])

    def declaration_conflict(self, text):
        """Check affirmative claims separately; a disclaimer cannot license a later claim."""
        if not isinstance(text, str):
            return ""
        text = re.sub(r'(?:原文(?:写着|写道|为)|引用|措辞为)[：:]?\s*[“「][^”」]*[”」]', '', text)
        # Commas inside an explicit row total are numeric separators, not
        # independent claims (e.g. 合计7,445条).
        text = re.sub(r"((?:合计|总计)\s*)(\d{1,3}(?:[,，]\d{3})+)(\s*[条行笔项])",
                      lambda match: match[1] + re.sub(r"[,，]", "", match[2]) + match[3], text)
        for clause, table_header in _claim_units(text):
            clause = clause.strip()
            # Only explicit scope exclusions are exempt. Do not skip an entire
            # answer or a clause containing a later affirmative assertion.
            clause = re.sub(r'(?:尚未|还未|没有|未能|无法|不能)(?:完成)?(?:完整读取|读完|读取全部内容|完整分析)', '', clause)
            clause = re.sub(r'(?:不代表|不涵盖|不包含|未覆盖|不涉及)(?:全部|所有)工作表', '', clause)
            conflict = self._clause_conflict(clause, table_header=table_header)
            if conflict:
                return conflict
        return ""

    def _clause_conflict(self, text, *, table_header=""):
        """Raw coverage and scoped aggregate evidence prove different claims."""
        if not isinstance(text, str) or not any(key in text for key in FULL_CLAIM_KEYWORDS):
            return ""
        claim_all = any(key in text for key in ALL_SHEET_KEYWORDS)
        if claim_all and self.failures:
            return "DOCUMENT_READ_INCOMPLETE"
        for doc in self.documents.values():
            if not doc.structured:
                if not doc.complete:
                    return "DOCUMENT_READ_INCOMPLETE"
                continue
            if claim_all and not doc.inventory_complete:
                return "DOCUMENT_READ_INCOMPLETE"
            named = _mentioned_names(doc.inventory, text)
            required = set(doc.inventory) if claim_all else (named or doc.touched)
            if not required:
                return "DOCUMENT_READ_INCOMPLETE"
            if all(doc.sheet_full(name) for name in required):
                continue
            if not _statistic_supported(doc, required, text, claim_all, table_header=table_header):
                return "DOCUMENT_READ_INCOMPLETE"
        return ""

    def independent_scopes(self):
        """Only full documents, whole sheets or verified statistics support a partial handoff."""
        scopes = []
        for doc in self.documents.values():
            if doc.error:
                continue
            if doc.complete:
                scopes.append({'file_id': doc.file_id, 'kind': 'document'})
            elif doc.structured:
                for sheet in doc.inventory:
                    if doc.inventory[sheet] > 0 and doc.sheet_full(sheet):
                        scopes.append({'file_id': doc.file_id, 'kind': 'sheet', 'sheet': sheet})
                if doc.aggregates:
                    scopes.append({'file_id': doc.file_id, 'kind': 'statistics'})
        return scopes

    def unfinished_scopes(self):
        gaps = []
        known_files = set()
        for ref, doc in self.documents.items():
            known_files.add(doc.file_id)
            if doc.complete:
                continue
            if doc.structured:
                for sheet in doc.inventory:
                    if not doc.sheet_full(sheet):
                        gaps.append({'kind': 'sheet', 'file_id': doc.file_id, 'target': sheet, 'impact': 'scope_unread'})
            else:
                gaps.append({'kind': 'document', 'file_id': doc.file_id, 'impact': 'scope_unread'})
        for key in self.failures:
            if key.startswith('parse:') and key[6:] not in known_files:
                gaps.append({'kind': 'document', 'file_id': key[6:], 'impact': 'scope_unread'})
        return gaps

    def permits_scoped_answer(self, text):
        if any(code in FILE_POLICY_CODES or code == 'MINERU_SUBMIT_AMBIGUOUS' for code in self.failures.values()):
            return False
        return bool(self.independent_scopes() and self.unfinished_scopes() and text.strip()
                    and not self.declaration_conflict(text))

    def recover_sources(self, file_ids, *, preserve_file_id=""):
        for file_id in file_ids:
            if self.failures.get("parse:" + file_id) in {"FILE_ACCESS_DENIED", "FILE_REF_INVALID", "FILE_REF_EXPIRED"}:
                continue
            self.failures.pop("parse:" + file_id, None)
            for ref, doc in list(self.documents.items()):
                if doc.file_id == file_id and file_id != preserve_file_id:
                    del self.documents[ref]
                    self.failures.pop("read:" + ref, None)
                    self._clear_aggregate_reference(ref)


_STATISTIC_WORDS = {"sum": ("合计", "总计", "总和"), "avg": ("平均",), "median": ("中位数",),
                    "count": ("数量", "计数"), "count_distinct": ("去重数量",), "min": ("最小",), "max": ("最大",)}


def _claim_units(text):
    """Keep a Markdown table with its own header; never borrow adjacent prose."""
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        header = lines[index].strip()
        if (header.startswith('|') and header.endswith('|') and index + 1 < len(lines)
                and re.fullmatch(r'\s*\|(?:\s*:?-{3,}:?\s*\|)+\s*', lines[index + 1])
                and header.count('|') == lines[index + 1].count('|')):
            end = index + 2
            while end < len(lines) and lines[end].strip().startswith('|') and lines[end].strip().endswith('|'):
                end += 1
            yield '\n'.join(lines[index:end]), header
            index = end
        else:
            for clause in re.split(r'[。！？；;，,]|但是|但|然而', lines[index]):
                yield clause, ''
            index += 1


def _mentioned_names(names, text):
    # Longest names first: 金额#2 must not also match the distinct 金额 column.
    found = set()
    for name in sorted(names, key=len, reverse=True):
        if name in text:
            found.add(name)
            text = text.replace(name, "")
    return found


def _table_columns(header, names):
    """Resolve display labels like 金额合计（元） against actual column names.

    Do not strip units or guess aliases: only an inserted statistic word is
    removable, and ambiguous labels remain unsupported.
    """
    resolved, functions = set(), set()
    for cell in header.strip('|').split('|'):
        label = cell.strip().strip('*').strip()
        if label in names:
            resolved.add(label)
            continue
        candidates = set()
        for name in names:
            unit = re.search(r'[（(][^（）()]+[）)]$', name)
            stem, suffix = (name[:unit.start()], unit[0]) if unit else (name, '')
            for fn, words in _STATISTIC_WORDS.items():
                if any(label in (word + name, name + word, stem + word + suffix) for word in words):
                    candidates.add((name, fn))
        if len(candidates) == 1:
            functions.update(candidates)
            resolved.update(name for name, _ in candidates)
        elif candidates:
            return None
        elif any(word in label for words in _STATISTIC_WORDS.values() for word in words):
            return None
    return resolved, functions


def _statistic_supported(doc, required, text, claim_all, *, table_header=""):
    if any(word in text for word in ("读完", "完整读取", "全部内容", "全文", "全量明细", "完整分析")):
        return False
    for evidence in doc.aggregates:
        sources = evidence["sources"]
        if {source["sheet"] for source in sources if doc.inventory[source["sheet"]] > 0} != {name for name in required if doc.inventory[name] > 0}:
            continue
        rule = evidence.get("filter")
        if rule:
            # Filtered totals need an explicit filter in the claim, and can
            # never establish an unqualified full-sheet/workbook conclusion.
            words = {"eq": ("等于", "为", "="), "ne": ("不等于", "不为", "!="),
                     "gt": ("大于", ">"), "gte": ("大于等于", ">="), "lt": ("小于", "<"),
                     "lte": ("小于等于", "<="), "contains": ("包含",)}.get(rule.get("op"), ())
            if claim_all or not any(str(rule.get("column")) + word + str(rule.get("value")) in text for word in words):
                continue
        if any(source["range"] != [1, doc.inventory[source["sheet"]]] and
               not re.search(r"第?" + str(source["range"][0]) + r"[–—~至-]" + str(source["range"][1]) + r"行", text)
               for source in sources):
            continue
        metrics = evidence.get("metrics") or []
        groups = evidence.get("group_by") or []
        group_scope = table_header or text
        if groups and ((not table_header and not any(word in text for word in ("分组", "按", "汇总", "统计", "分布")))
                       or any(group not in group_scope for group in groups)):
            continue
        allowed = {m.get("column") for m in metrics} | set(groups)
        if rule:
            allowed.add(rule.get("column"))
        column_names = doc.column_names | {name for name in allowed if isinstance(name, str)}
        mentioned_columns = _mentioned_names(column_names, text.replace(table_header, '', 1) if table_header else text)
        if table_header:
            table_columns = _table_columns(table_header, column_names)
            if table_columns is None:
                continue
            names, functions = table_columns
            if not functions.issubset({(m.get('column'), m.get('fn')) for m in metrics}):
                continue
            mentioned_columns.update(names)
        if mentioned_columns - allowed:
            continue
        # Normalize only the adjacent count-total phrase. An additional sum
        # claim elsewhere in this clause must still have independent evidence.
        function_text = re.sub(r"(数量|计数)(?:合计|总计)", r"\1", text)
        row_totals = re.findall(r"(?:合计|总计)\s*(\d{1,12})\s*[条行笔项]", text)
        if row_totals:
            function_text = re.sub(r"(?:合计|总计)\s*\d{1,12}\s*[条行笔项]", "计数", function_text)
        claimed_functions = {fn for fn, words in _STATISTIC_WORDS.items() if any(word in function_text for word in words)}
        if "去重数量" in text and "数量" not in text.replace("去重数量", ""):
            claimed_functions.discard("count")
        if not claimed_functions.issubset({m.get("fn") for m in metrics}):
            continue
        if (row_totals and type(evidence.get("rows_matched")) is int
                and any(int(value) != evidence["rows_matched"] for value in row_totals)):
            continue
        if (row_totals and not groups and claimed_functions == {"count"}
                and type(evidence.get("rows_matched")) is int
                and all(int(value) == evidence["rows_matched"] for value in row_totals)):
            return True
        mentioned = [metric for metric in metrics if metric.get("column") in mentioned_columns]
        if mentioned and all(any(word in text for word in _STATISTIC_WORDS.get(metric.get("fn"), ())) for metric in mentioned):
            return True
    return False
