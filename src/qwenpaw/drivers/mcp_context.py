"""Per-invocation MCP transport metadata, separate from model arguments."""
from contextlib import contextmanager
from contextvars import ContextVar

_metadata = ContextVar("mcp_call_metadata", default=None)

def current_mcp_metadata():
    value = _metadata.get()
    return dict(value) if value else None

@contextmanager
def mcp_call_metadata(value):
    token = _metadata.set(dict(value))
    try:
        yield
    finally:
        _metadata.reset(token)
