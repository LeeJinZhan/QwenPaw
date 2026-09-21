"""One-use, task-bound grants issued after Runtime Tool Gateway approval.

The loopback MCP listener shares this process registry with the bank plugin.
A signed document reference locates cached data; it is never authorization.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
import secrets
import time

from qwenpaw.drivers.mcp_context import mcp_call_metadata

DOCUMENT_TOOLS = frozenset({"parse_documents", "read_document_chunks", "read_range", "aggregate", "search"})
_DEFAULTS = {
    "parse_documents": {"parse_method": "auto", "language": "auto", "options": None},
    "read_document_chunks": {"cursor": None, "limit": 5},
    "read_range": {"sheet": None, "rows": None, "row_cursor": None, "columns": None, "format": "markdown", "include_header": True},
    "aggregate": {}, "search": {"sheet": None, "limit": 100},
}
_KEY = "bank_runtime_document_grant"
_grants = {}
_task = ContextVar("authorized_document_task", default="")

class DocumentAccessError(RuntimeError):
    code = error_code = "FILE_ACCESS_DENIED"

    def __init__(self):
        super().__init__("Document access requires current task authorization")

def _digest(name, arguments):
    if name not in DOCUMENT_TOOLS or not isinstance(arguments, dict):
        raise DocumentAccessError()
    value = {**_DEFAULTS[name], **arguments}
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).digest()

@contextmanager
def approved_document_grant(task_id, name, arguments):
    if not isinstance(task_id, str) or not task_id:
        raise DocumentAccessError()
    now = time.monotonic()
    for key, value in list(_grants.items()):
        if value[3] <= now:
            _grants.pop(key, None)
    nonce = secrets.token_urlsafe(32)
    _grants[nonce] = (task_id, name, _digest(name, arguments), now + 60)
    metadata = {_KEY: nonce}
    try:
        yield metadata
    finally:
        _grants.pop(nonce, None)


@contextmanager
def approved_document_call(task_id, name, arguments):
    with approved_document_grant(task_id, name, arguments) as metadata:
        with mcp_call_metadata(metadata):
            yield metadata

@contextmanager
def consume_document_call(metadata, name, arguments):
    if hasattr(metadata, "model_dump"):
        metadata = metadata.model_dump()
    nonce = metadata.get(_KEY) if isinstance(metadata, dict) else None
    grant = _grants.pop(nonce, None) if isinstance(nonce, str) else None
    if not grant or grant[1] != name or grant[2] != _digest(name, arguments) or grant[3] <= time.monotonic():
        raise DocumentAccessError()
    token = _task.set(grant[0])
    try:
        yield
    finally:
        _task.reset(token)

def current_document_task():
    task_id = _task.get()
    if not task_id:
        raise DocumentAccessError()
    return task_id
