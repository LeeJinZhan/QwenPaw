"""Optional scoped, content-free receipts for individual model invocations."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Callable
from uuid import uuid4

_observer: ContextVar[Callable | None] = ContextVar("model_request_observer", default=None)
_MAX_TOKENS = 9_007_199_254_740_991


@contextmanager
def observe_model_requests(callback):
    token = _observer.set(callback)
    try:
        yield
    finally:
        _observer.reset(token)


class ModelRequestObservation:
    """Capture the observer at invocation, including when a stream later closes."""

    def __init__(self, provider_id, model_id):
        self.callback = _observer.get()
        self.request_id = uuid4().hex
        self.provider_id = provider_id
        self.model_id = model_id
        self.started = datetime.now(timezone.utc)
        self.finished = False

    def finish(self, usage, status):
        if self.finished or self.callback is None:
            return
        self.finished = True
        prompt = getattr(usage, "input_tokens", None)
        completion = getattr(usage, "output_tokens", None)
        reported = (type(prompt) is int and type(completion) is int
                    and 0 <= prompt <= _MAX_TOKENS and 0 <= completion <= _MAX_TOKENS
                    and prompt + completion <= _MAX_TOKENS)
        event = {
            "request_id": self.request_id,
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "started_at_utc": self.started.isoformat(timespec="microseconds").replace("+00:00", "Z"),
            "ended_at_utc": datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z"),
            "terminal_status": status,
            "usage_state": "reported" if reported else "unknown",
            "input_tokens": prompt if reported else None,
            "output_tokens": completion if reported else None,
        }
        try:
            self.callback(event)
        except Exception:
            # Optional accounting must never change model execution or expose
            # provider bodies through exception logging.
            pass
