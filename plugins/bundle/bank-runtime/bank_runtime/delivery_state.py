"""Producer-local evidence shared by trusted middleware and the SSE boundary."""
from contextvars import ContextVar
from contextlib import contextmanager
from dataclasses import dataclass, field

@dataclass
class DeliveryState:
    task_id: str
    analysis: dict = field(default_factory=dict)

_STATE = ContextVar('bank_runtime_delivery_state', default=None)

def current_delivery_state():
    return _STATE.get()

def begin_delivery_state(task_id):
    state = DeliveryState(task_id)
    return state, _STATE.set(state)

def end_delivery_state(token):
    _STATE.reset(token)

@contextmanager
def delivery_scope(state):
    token = _STATE.set(state)
    try:
        yield
    finally:
        _STATE.reset(token)
