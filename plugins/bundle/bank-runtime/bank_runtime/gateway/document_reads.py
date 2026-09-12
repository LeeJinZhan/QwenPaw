"""Request-local read evidence from admitted tool results, never model text."""
from collections.abc import Mapping
from dataclasses import dataclass, field
import hashlib
import json

from .completion import _result_values

READ_ERROR_CODES = frozenset({
    "DOCUMENT_READ_INCOMPLETE", "DOCUMENT_READ_NO_PROGRESS", "DOCUMENT_PARSE_FAILED",
    "DOCUMENT_REF_EXPIRED", "DOCUMENT_RESULT_TOO_LARGE", "DOCUMENT_TEXT_TRUNCATED",
    "DOCUMENT_TEXT_ENCODING_UNSUPPORTED", "MINERU_TIMEOUT", "MINERU_UNAVAILABLE", "MINERU_SUBMIT_AMBIGUOUS",
})
FILE_POLICY_CODES = frozenset({"FILE_ACCESS_DENIED", "FILE_REF_INVALID", "FILE_REF_EXPIRED", "FILE_TYPE_UNSUPPORTED"})


def is_read_recovery_tool(name):
    return name.endswith(("parse_documents", "read_document_chunks")) or name in {
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
        candidates = [value, *items] if isinstance(items, list) else [value]
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
    error: str = ""

    @property
    def complete(self):
        return not self.error and self.terminal and len(self.chunks) == self.total


class DocumentReadLedger:
    def __init__(self):
        self.documents: dict[str, _Read] = {}
        self.failures: dict[str, str] = {}
        self.attempts: dict[str, int] = {}

    @property
    def pending(self):
        return bool(self.failures) or any(not doc.complete for doc in self.documents.values())

    @property
    def error_code(self):
        if any(code in FILE_POLICY_CODES for code in self.failures.values()):
            # Preserve the existing denial/unknown-result presentation and do
            # not turn a scope rejection into a retryable read failure.
            return "ARTIFACT_OUTPUT_MISSING"
        if "MINERU_SUBMIT_AMBIGUOUS" in self.failures.values():
            return "MINERU_SUBMIT_AMBIGUOUS"
        if any(self.attempts.get(key, 0) >= 3 for key in self.failures):
            return "DOCUMENT_READ_NO_PROGRESS"
        for doc in self.documents.values():
            if doc.no_progress >= 3:
                return "DOCUMENT_READ_NO_PROGRESS"
        return next(iter(self.failures.values()), "") or next(
            (doc.error for doc in self.documents.values() if doc.error), "DOCUMENT_READ_INCOMPLETE")

    def coverage(self, ref):
        doc = self.documents[ref]
        return len(doc.chunks), doc.total

    def start(self, name, payload):
        if name.endswith("parse_documents"):
            for item in payload.get("documents", []):
                if isinstance(item, Mapping):
                    key = "parse:" + str(item.get("file_id") or "")
                    self.failures[key] = "DOCUMENT_PARSE_FAILED"
                    self.attempts[key] = self.attempts.get(key, 0) + 1
        elif name.endswith("read_document_chunks"):
            key = "read:" + str(payload.get("document_ref") or "")
            self.failures[key] = "DOCUMENT_READ_INCOMPLETE"
            self.attempts[key] = self.attempts.get(key, 0) + 1

    def observe(self, name, payload, content, success):
        values = result_objects(content)
        code = result_error(content)
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
                if mode not in (None, "inline", "chunked"):
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

    def recover_sources(self, file_ids, *, preserve_file_id=""):
        for file_id in file_ids:
            self.failures.pop("parse:" + file_id, None)
            for ref, doc in list(self.documents.items()):
                if doc.file_id == file_id and file_id != preserve_file_id:
                    del self.documents[ref]
                    self.failures.pop("read:" + ref, None)
