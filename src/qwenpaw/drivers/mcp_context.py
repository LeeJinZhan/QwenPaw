"""Per-invocation MCP transport metadata, separate from model arguments."""
from contextlib import contextmanager
from contextvars import ContextVar

_metadata = ContextVar("mcp_call_metadata", default=None)
_request_observer = ContextVar("mcp_request_observer", default=None)

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


@contextmanager
def observe_mcp_requests(observer):
    """Observe physical requests for an opted-in caller, without changing MCP wire data."""
    token = _request_observer.set(observer)
    try:
        yield
    finally:
        _request_observer.reset(token)


def current_mcp_request_observer():
    return _request_observer.get()

_timeout = ContextVar("mcp_call_timeout", default=None)

def current_mcp_timeout():
    return _timeout.get()

@contextmanager
def mcp_call_timeout(seconds):
    """Local caller budget, never serialized into model arguments or metadata."""
    from datetime import timedelta
    import math
    if seconds is not None and (not math.isfinite(seconds) or seconds <= 0):
        raise TimeoutError('MCP task budget exhausted')
    token = _timeout.set(timedelta(seconds=min(seconds, 3600)) if seconds is not None else None)
    try:
        yield
    finally:
        _timeout.reset(token)
