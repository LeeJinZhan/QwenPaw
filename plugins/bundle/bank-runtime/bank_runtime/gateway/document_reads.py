"""Request-local read evidence from admitted tool results, never model text."""
from collections.abc import Mapping
from dataclasses import dataclass, field
import hashlib
import json

from .completion import _result_values

READ_ERROR_CODES = frozenset({
    "DOCUMENT_ARGUMENT_INVALID", "DOCUMENT_FORMULA_CACHE_MISSING",
    "DOCUMENT_READ_INCOMPLETE", "DOCUMENT_READ_NO_PROGRESS", "DOCUMENT_PARSE_FAILED",
    "DOCUMENT_REF_EXPIRED", "DOCUMENT_RESULT_TOO_LARGE", "DOCUMENT_TEXT_TRUNCATED",
    "DOCUMENT_TEXT_ENCODING_UNSUPPORTED", "MINERU_TIMEOUT", "MINERU_UNAVAILABLE", "MINERU_SUBMIT_AMBIGUOUS",
    "DOCUMENT_ENGINE_UNAVAILABLE", "DOCUMENT_QUEUE_TIMEOUT", "DOCUMENT_EXECUTION_TIMEOUT", "DOCUMENT_ANALYSIS_FAILED",
})
FILE_POLICY_CODES = frozenset({"FILE_ACCESS_DENIED", "FILE_REF_INVALID", "FILE_REF_EXPIRED", "FILE_TYPE_UNSUPPORTED"})
UNRESOLVED_REMOTE_CODES = frozenset({"MINERU_TIMEOUT", "MINERU_SUBMIT_AMBIGUOUS"})


def _sheet_metadata(value):
    metadata={key:value[key] for key in ('name','rows','cols','source_rows','header_row','hidden') if key in value}
    columns=[entry['name'][:255] for entry in value.get('columns',[])
             if isinstance(entry,Mapping) and isinstance(entry.get('name'),str)]
    metadata['columns']=columns[:128]
    metadata['columns_complete']=bool(value.get('columns_complete')) and len(columns)<=128
    return metadata


def is_read_recovery_tool(name):
    return name.endswith((
        "parse_documents", "read_document_chunks", "read_range", "aggregate", "search", "analyze",
    )) or name in {
        "Skill", "artifact_convert", "runtime_sandbox_files_search", "runtime_sandbox_files_select",
    }


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
    inventory_cursor: int | None = None
    inventory_total: int = 0
    column_names: set[str] = field(default_factory=set)
    covered: dict[str, list] = field(default_factory=dict)
    source_inventory: dict[str, int] = field(default_factory=dict)
    source_covered: dict[str, list] = field(default_factory=dict)
    sheet_metadata: dict[str, dict] = field(default_factory=dict)
    aggregates: list[dict] = field(default_factory=list)
    aggregate_pages: dict[str, list[list[int]]] = field(default_factory=dict)
    aggregate_stalls: dict[str, int] = field(default_factory=dict)
    repeated_statistics: bool = False
    touched: set = field(default_factory=set)
    cell_fragments: set[str] = field(default_factory=set)
    projections: set[str] = field(default_factory=set)
    read_ranges: list[dict] = field(default_factory=list)
    metadata_columns: dict[str, dict[int,str]] = field(default_factory=dict)

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
        return self._covered_count(self.covered.get(name, []))

    @staticmethod
    def _covered_count(ranges) -> int:
        merged: list[list[int]] = []
        for start, end in sorted(ranges):
            if merged and start <= merged[-1][1] + 1:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        return sum(end - start + 1 for start, end in merged if end >= start)

    def source_full(self, name):
        total = self.source_inventory.get(name)
        return total is not None and self._covered_count(self.source_covered.get(name, [])) >= total

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
        self.aggregate_argument_scopes: dict[str, object] = {}
        self.request_results: dict[str, tuple[str, int]] = {}
        self.request_errors: dict[str, str] = {}
        self.reference_argument_failures: dict[str, int] = {}
        self.call_argument_failures: dict[tuple[str, str], int] = {}

    def reject_call_arguments(self, name, payload):
        key = (name, str(payload.get('document_ref') or ''))
        self.call_argument_failures[key] = self.call_argument_failures.get(key, 0) + 1

    @staticmethod
    def reference_operation_key(name, payload):
        from .document_access import document_call_parameters
        value = document_call_parameters(name.removeprefix("MinerU__"), payload)
        value.pop("document_ref", None)
        return name + json.dumps(value, sort_keys=True, ensure_ascii=False)

    def reject_reference_argument(self, name, payload):
        key = self.reference_operation_key(name, payload)
        self.reference_argument_failures[key] = self.reference_argument_failures.get(key, 0) + 1

    def recover_reference_argument(self, name, payload, content):
        ref = payload.get("document_ref")
        doc = self.documents.get(ref)
        if doc is None or doc.error or "read:" + str(ref) in self.failures:
            return
        if name.endswith("aggregate") and self._aggregate_keys(payload).intersection(self.failures):
            return
        verified = name.endswith(("read_range", "read_document_chunks", "aggregate"))
        if name.endswith(("search", "analyze")):
            values = result_objects(content)
            verified = (len(values) == 1 and values[0].get("document_ref") == ref
                        and not result_error(content))
            if verified and name.endswith("search"):
                verified = isinstance(values[0].get("hits"), list)
            elif verified:
                verified = (values[0].get("engine") == "table-facts-3"
                            and isinstance(values[0].get("evidence"), list))
        if verified:
            self.reference_argument_failures.pop(self.reference_operation_key(name, payload), None)
            if not name.endswith('aggregate') or doc.aggregates:
                self.call_argument_failures.pop((name, str(ref)), None)

    def repeated_request(self, name, payload):
        from .document_access import invalid_document_reference_argument
        if (invalid_document_reference_argument(name, payload)
                and self.reference_argument_failures.get(self.reference_operation_key(name, payload), 0) >= 2):
            return "DOCUMENT_ARGUMENT_INVALID"
        if name.endswith('parse_documents'):
            for source in payload.get('documents', []):
                if isinstance(source, Mapping):
                    code = self.failures.get('parse:' + str(source.get('file_id') or ''), '')
                    if code in UNRESOLVED_REMOTE_CODES:
                        return code
        key = name + json.dumps(payload, sort_keys=True, ensure_ascii=False)
        error = self.request_errors.get(key, '')
        if error == 'MINERU_SUBMIT_AMBIGUOUS' or error in FILE_POLICY_CODES:
            return error
        if self.request_results.get(key, ('', 0))[1] >= (2 if error == 'DOCUMENT_ARGUMENT_INVALID' else 3):
            return error or 'DOCUMENT_READ_NO_PROGRESS'
        return ''

    @property
    def pending(self):
        # Completed statistics never grant an exemption to unfinished reads
        # or other aggregate operations. Their progress budgets are separate.
        return bool(self.failures) or bool(self.reference_argument_failures) or bool(self.call_argument_failures) or any(
            (not doc.complete and (not doc.structured or (doc.no_progress >= 3 and not doc.last_range_complete)))
            or bool(doc.aggregate_stalls)
            for doc in self.documents.values()
        )

    @property
    def argument_retry_exhausted(self):
        return any(count >= 2 for count in self.call_argument_failures.values()) or any(self.argument_failures.get(key, 0) >= 2
                   for key, code in self.failures.items() if code == "DOCUMENT_ARGUMENT_INVALID")

    @property
    def error_code(self):
        if any(code in FILE_POLICY_CODES for code in self.failures.values()):
            # Preserve the existing denial/unknown-result presentation and do
            # not turn a scope rejection into a retryable read failure.
            return "ARTIFACT_OUTPUT_MISSING"
        if "MINERU_SUBMIT_AMBIGUOUS" in self.failures.values():
            return "MINERU_SUBMIT_AMBIGUOUS"
        for key, code in self.failures.items():
            if key.startswith('aggregate:') and code not in {'DOCUMENT_ARGUMENT_INVALID', 'DOCUMENT_READ_INCOMPLETE'}:
                # Later schema errors cannot replace the diagnostic of an
                # independently unresolved computation that actually ran.
                return code
        if "DOCUMENT_ARGUMENT_INVALID" in self.failures.values():
            return "DOCUMENT_ARGUMENT_INVALID"
        if self.reference_argument_failures or self.call_argument_failures:
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

    @staticmethod
    def _aggregate_argument_scopes(payload):
        ref = str(payload.get('document_ref') or '')
        ops = payload.get('ops')
        if not isinstance(ops, list) or not ops:
            ops = [ops]
        scopes = {}
        for op in ops:
            if isinstance(op, Mapping):
                scope = {k:v for k,v in DocumentReadLedger._aggregate_scope(op).items()
                         if k in {'sheet','metrics','group_by','filter','row_range','cross_sheet_union','numeric_text'}}
                # A schema rejection executed no metric. Allow correcting its
                # fn/op spelling, but preserve metric columns, computation,
                # sheet, range, filters and grouping as independent scopes.
                metrics = scope.get('metrics')
                if isinstance(metrics, list) and metrics:
                    scope['metrics'] = [{'column':m.get('column'), 'fn':str(m.get('fn',m.get('op')) or '').lower()}
                                        if isinstance(m, Mapping) else None for m in metrics]
                    functions={'sum','avg','count','count_rows','count_nonempty','count_numeric','count_distinct','min','max','median'}
                    for metric in scope['metrics']:
                        if isinstance(metric, dict) and metric['fn'] not in functions:
                            metric['fn']=None
                    for original, metric in zip(metrics, scope['metrics']):
                        if (isinstance(original, Mapping) and isinstance(metric, dict)
                                and 'fn' in original and 'op' in original
                                and original['fn'] != original['op']):
                            metric['fn'] = None
                else:
                    scope['metrics'] = None
                scope.setdefault('numeric_text', 'strict')
            else:
                scope = op
            digest = hashlib.sha256(json.dumps(scope, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            scopes[f'aggregate-argument:{ref}:{digest}'] = scope
        return scopes

    @staticmethod
    def _aggregate_argument_keys(payload):
        return set(DocumentReadLedger._aggregate_argument_scopes(payload))

    @staticmethod
    def _repairs_aggregate_arguments(failed, corrected):
        if not isinstance(failed, Mapping) or not isinstance(corrected, Mapping):
            return failed == corrected
        before = {k:v for k,v in failed.items() if k != 'metrics'}
        after = {k:v for k,v in corrected.items() if k != 'metrics'}
        if 'sheet' not in before:
            after.pop('sheet', None)
        if before != after:
            return False
        metrics = failed.get('metrics')
        actual = corrected.get('metrics')
        if metrics is None:
            return True
        if not isinstance(actual, list) or len(metrics) != len(actual):
            return False
        # Unknown fields may be filled only at their original position. Keep
        # every known sibling column/function and all other operation scope.
        return all(expected is None or (isinstance(value, Mapping) and all(
            field is None or value.get(key) == field for key, field in expected.items()))
            for expected, value in zip(metrics, actual))

    def _fail_aggregate(self, payload, code):
        keys = self._aggregate_keys(payload)
        if code == "DOCUMENT_ARGUMENT_INVALID":
            # Schema rejection did not execute an operation. Keep the existing
            # bounded parameter correction, without erasing prior real errors.
            for key in keys:
                if self.failures.get(key) == "DOCUMENT_READ_INCOMPLETE" and self.attempts.get(key) == 1:
                    self.failures.pop(key, None)
                    self.attempts.pop(key, None)
            scopes = self._aggregate_argument_scopes(payload)
            self.aggregate_argument_scopes.update(scopes)
            for key in scopes:
                self.failures[key] = code
            return
        for key in keys:
            self.failures[key] = code

    def _clear_aggregate_reference(self, ref, replacement_ref=None):
        for collection in (self.failures, self.attempts, self.argument_failures, self.aggregate_argument_scopes):
            for key in list(collection):
                if key.startswith((f"aggregate:{ref}:", f"aggregate-argument:{ref}:")):
                    collection.pop(key, None)
        # Schema failures never executed. Carry their bounded retry budget to
        # a trusted reparse of the same file, so a corrected current call can
        # recover without either stranding an old reference or resetting it.
        for key in list(self.call_argument_failures):
            if key[1] == ref:
                count = self.call_argument_failures.pop(key)
                if replacement_ref:
                    replacement = (key[0], replacement_ref)
                    self.call_argument_failures[replacement] = max(
                        count, self.call_argument_failures.get(replacement, 0))

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
        code = result_error(content)
        if code in FILE_POLICY_CODES and name in {'MinerU__' + tool for tool in ('read_range', 'read_document_chunks', 'aggregate', 'search', 'analyze')}:
            from .document_access import document_call_parameters
            scope = hashlib.sha256(json.dumps(document_call_parameters(name.removeprefix('MinerU__'), payload),
                sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            # A denial belongs to this operation, not the shared read-progress
            # key. Successful reads or reparsing cannot erase it.
            self.failures['policy:' + name + ':' + scope] = code
        if name.endswith("analyze"):
            values = result_objects(content)
            ref = payload.get("document_ref")
            if (not success or len(values) != 1 or values[0].get("document_ref") != ref
                    or values[0].get("engine") != "table-facts-3" or not isinstance(values[0].get("evidence"), list)):
                self.failures["read:" + str(ref or "")] = result_error(content) or "DOCUMENT_ANALYSIS_FAILED"
                return
            # Only the trusted RPC parent's observations count. The arbitrary
            # user_program result, including any fabricated totals, is ignored.
            for entry in values[0]["evidence"]:
                if not isinstance(entry, Mapping) or entry.get("name") not in {"read_range", "aggregate"}:
                    continue
                arguments = {"document_ref": ref, **entry.get("arguments", {})}
                self.observe("MinerU__" + entry["name"], arguments,
                    [{"type": "text", "text": json.dumps(entry.get("result", {}), ensure_ascii=False)}], True)
            return
        if name.endswith(('read_document_chunks', 'read_range', 'aggregate', 'parse_documents')):
            key = name + json.dumps(payload, sort_keys=True, ensure_ascii=False)
            result = hashlib.sha256(json.dumps(result_objects(content), sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            previous, count = self.request_results.get(key, ('', 0))
            self.request_results[key] = (result, count + 1 if previous == result else 1)
            self.request_errors[key] = result_error(content)
        values = result_objects(content)
        code = result_error(content)
        if code == "DOCUMENT_ARGUMENT_INVALID" and name.endswith(("parse_documents", "read_range", "aggregate", "read_document_chunks")):
            keys = (["parse:" + str(item.get("file_id") or "") for item in payload.get("documents", []) if isinstance(item, Mapping)]
                    if name.endswith("parse_documents") else self._aggregate_argument_keys(payload)
                    if name.endswith('aggregate') else ['read:' + str(payload.get('document_ref') or '')])
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
                if ((payload.get("options") or {}).get("image_text") is True
                        and isinstance(item.get("coverage"), Mapping)
                        and item["coverage"].get("image_text") not in {"parsed", "not_present"}):
                    batches = item.get('ocr_batches')
                    stop = batches.get('stop_reason') if isinstance(batches, Mapping) else None
                    self.failures["parse:" + file_id] = (
                        stop if stop in UNRESOLVED_REMOTE_CODES else "DOCUMENT_READ_INCOMPLETE")
                    continue
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
                            self._clear_aggregate_reference(old_ref, ref)
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
                        inventory_cursor=raw_inventory.get('next_inventory_cursor'),
                        inventory_total=raw_inventory.get('sheet_count',len(inventory)),
                        column_names={column["name"] for sheet in raw_inventory.get("sheets", [])
                                      if isinstance(sheet, Mapping) for column in sheet.get("columns", [])
                                      if isinstance(column, Mapping) and isinstance(column.get("name"), str)},
                        source_inventory={sheet['name']:sheet['source_rows'] for sheet in raw_inventory.get('sheets', [])
                                          if isinstance(sheet, Mapping) and sheet.get('name') in inventory
                                          and type(sheet.get('source_rows')) is int and sheet['source_rows'] >= 0},
                        sheet_metadata={sheet['name']:_sheet_metadata(sheet)
                            for sheet in raw_inventory.get('sheets',[]) if isinstance(sheet,Mapping)
                            and isinstance(sheet.get('name'),str)},
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
                        self._clear_aggregate_reference(old_ref, ref)
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
            before = (dict(doc.inventory), set(doc.column_names), doc.inventory_complete,
                json.dumps(doc.sheet_metadata,sort_keys=True))
            inventory = value.get("inventory")
            if isinstance(inventory, Mapping):
                for meta in inventory.get("sheets", []):
                    if isinstance(meta, Mapping) and isinstance(meta.get("name"), str) and type(meta.get("rows")) is int:
                        if meta["name"] in doc.inventory and doc.inventory[meta["name"]] != meta["rows"]:
                            doc.error = "DOCUMENT_READ_INCOMPLETE"
                            self.failures[key] = doc.error
                            return
                        doc.inventory[meta["name"]] = meta["rows"]
                        doc.sheet_metadata[meta['name']]=_sheet_metadata(meta)
                        if type(meta.get('source_rows')) is int and meta['source_rows'] >= 0:
                            if (meta['name'] in doc.source_inventory
                                    and doc.source_inventory[meta['name']] != meta['source_rows']):
                                doc.error = 'DOCUMENT_READ_INCOMPLETE'
                                return
                            doc.source_inventory[meta['name']] = meta['source_rows']
                        doc.column_names.update(c["name"] for c in meta.get("columns", []) if isinstance(c, Mapping) and isinstance(c.get("name"), str))
                # All sheet names must be known, including earlier pages.
                doc.inventory_complete = len(doc.inventory) == inventory.get("sheet_count")
                doc.inventory_cursor=inventory.get('next_inventory_cursor')
                doc.inventory_total=inventory.get('sheet_count',doc.inventory_total)
            for meta in value.get("metadata", []):
                if isinstance(meta, Mapping) and meta.get("kind") == "column" and isinstance(meta.get("name"), str):
                    doc.column_names.add(meta["name"])
            sheet=value.get('sheet')
            start=payload.get('row_cursor',0)
            if sheet==payload.get('sheet') and sheet in doc.inventory and type(start) is int and start>=0:
                columns=doc.metadata_columns.setdefault(sheet,{})
                if start==0:
                    columns.clear()
                for meta in value.get('metadata',[]):
                    if (isinstance(meta,Mapping) and meta.get('kind')=='column'
                            and isinstance(meta.get('name'),str) and type(meta.get('source_index')) is int
                            and 0<=meta['source_index']<16384):
                        columns[meta['source_index']]=meta['name']
                metadata=doc.sheet_metadata.setdefault(sheet,{'name':sheet,'rows':doc.inventory[sheet]})
                metadata['columns']=[columns[i] for i in sorted(columns)]
                metadata['next_column_cursor']=value.get('next_inventory_cursor')
                total=metadata.get('cols')
                metadata['columns_complete']=(type(total) is int and 0<=total<=16384
                    and sorted(columns)==list(range(total)))
            advanced = before != (doc.inventory, doc.column_names, doc.inventory_complete,
                json.dumps(doc.sheet_metadata,sort_keys=True))
            doc.no_progress = 0 if advanced else doc.no_progress + 1
            self.failures.pop(key, None)
            self.attempts.pop(key, None)
            self.argument_failures.pop(key, None)
            return
        sheet = value.get("sheet")
        if payload.get("format") == "source" and value.get("coordinate_space") == "source":
            # Physical rows include title/header lines. They cannot be counted
            # against the analysis view's row totals or establish a full scan.
            if sheet in doc.inventory and isinstance(value.get("records"), list) and isinstance(value.get("signature"), str):
                signature = "source:" + hashlib.sha256(json.dumps([payload, value], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                advanced = signature not in doc.projections
                doc.projections.add(signature)
                doc.touched.add(sheet)
                requested = payload.get('rows')
                records = value['records']
                total = doc.source_inventory.get(sheet)
                returned = value.get('rows_returned')
                physical=value.get('source_columns')
                if (total is not None and isinstance(returned,list) and len(returned)==2
                        and all(type(r) is int for r in returned) and 1<=returned[0]<=returned[1]<=total
                        and value.get('sheet_total_rows')==total and isinstance(physical,list) and physical
                        and all(type(c) is int and 1<=c<=16384 for c in physical)
                        and len(set(physical))==len(physical)
                        and len(value.get('columns',[]))==len(physical)
                        and (not payload.get('columns') or value['columns']==payload['columns'])
                        and len(records)==returned[1]-returned[0]+1
                        and all(isinstance(record,Mapping) and record.get('source_row')==returned[0]+i
                            and isinstance(record.get('values'),list) and len(record['values'])==len(physical)
                            for i,record in enumerate(records))):
                    entry={'sheet':sheet,'rows':list(returned),'coordinate_space':'source',
                        'source_columns':list(physical)}
                    if entry not in doc.read_ranges:
                        doc.read_ranges.append(entry)
                if (total is not None and isinstance(returned, list) and len(returned) == 2
                        and all(type(row) is int for row in returned)
                        and 1 <= returned[0] <= returned[1] <= total
                        and value.get('sheet_total_rows') == total and value.get('all_columns') is True
                        and len(records) == returned[1] - returned[0] + 1
                        and all(isinstance(record, Mapping) and record.get('source_row') == returned[0] + index
                                and isinstance(record.get('values'), list) for index, record in enumerate(records))):
                    doc.source_covered.setdefault(sheet, []).append(list(returned))
                doc.last_range_complete = (
                    isinstance(requested, list) and len(requested) == 2
                    and all(type(row) is int for row in requested)
                    and requested[0] >= 1 and requested[1] >= requested[0]
                    and value.get('rows_returned') == requested
                    and value.get('has_more') is False and value.get('all_columns') is True
                    and len(records) == requested[1] - requested[0] + 1
                    and all(isinstance(record, Mapping) and record.get('source_row') == requested[0] + index
                            and isinstance(record.get('values'), list) for index, record in enumerate(records)))
                doc.no_progress = 0 if advanced else doc.no_progress + 1
                self.failures.pop(key, None)
                self.attempts.pop(key, None)
                self.argument_failures.pop(key, None)
            return
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
        corrected = self._aggregate_argument_scopes(payload).values()
        repaired = {key for key, scope in self.aggregate_argument_scopes.items()
                    if key.startswith(f'aggregate-argument:{ref}:')
                    and any(self._repairs_aggregate_arguments(scope, actual) for actual in corrected)}
        for key in self._aggregate_keys(payload) | repaired:
            self.failures.pop(key, None)
            self.attempts.pop(key, None)
            self.argument_failures.pop(key, None)
            self.aggregate_argument_scopes.pop(key, None)

    def _observe_search(self, payload, values, success):
        ref = str(payload.get("document_ref") or "")
        doc = self.documents.get(ref)
        if doc is None or not doc.structured or not success or not values:
            return
        for hit in (values[0].get("hits") or [])[:100]:
            if isinstance(hit, Mapping) and hit.get("sheet") in doc.inventory:
                doc.touched.add(hit["sheet"])

    def evidence_snapshot(self):
        """Describe observed scope; never claim to verify free-form assertions."""
        return {
            'documents': [{
                'file_id': doc.file_id, 'complete': doc.complete,
                'inventory_complete': doc.inventory_complete,
                'sheets': [{'name': name, 'total_rows': total,
                            'covered_ranges': [list(pair) for pair in doc.covered.get(name, [])],
                            'complete': doc.sheet_full(name),
                            'source_total_rows': doc.source_inventory.get(name),
                            'source_covered_ranges': [list(pair) for pair in doc.source_covered.get(name, [])],
                            'source_complete': doc.source_full(name)}
                           for name, total in doc.inventory.items()],
                'chunks_read': len(doc.chunks), 'chunks_total': doc.total,
                'statistics': [{key: value for key, value in evidence.items()
                                if key in {'sources', 'metrics', 'filter', 'group_by', 'rows_matched', 'semantics'}}
                               for evidence in doc.aggregates],
                'pending_statistics': bool(doc.aggregate_stalls),
                'read_ranges':doc.read_ranges,
            } for doc in self.documents.values()],
            'gaps': self.unfinished_scopes(),
        }

    def sources_complete(self, file_ids):
        for file_id in file_ids:
            matching = [doc for doc in self.documents.values() if doc.file_id == file_id]
            if not matching or not all(doc.complete for doc in matching):
                return False
            if 'parse:' + file_id in self.failures:
                return False
        return True

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
        return bool(self.independent_scopes() and self.unfinished_scopes() and text.strip())

    def recover_sources(self, file_ids, *, preserve_file_id=""):
        for file_id in file_ids:
            if self.failures.get("parse:" + file_id) in FILE_POLICY_CODES | UNRESOLVED_REMOTE_CODES:
                continue
            self.failures.pop("parse:" + file_id, None)
            for ref, doc in list(self.documents.items()):
                if doc.file_id == file_id and file_id != preserve_file_id:
                    del self.documents[ref]
                    self.failures.pop("read:" + ref, None)
                    self._clear_aggregate_reference(ref)
