"""Read-only provenance for requests actually emitted by CronExecutor.

Request JSON cannot set this task-local identity. Tokens are always reset when
execution finishes, including cancellation, and do not change scheduling.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CronExecutionIdentity:
    workspace: Any
    job_id: str


_identity: ContextVar[CronExecutionIdentity | None] = ContextVar(
    "cron_execution_identity", default=None
)


def get_cron_execution_identity() -> CronExecutionIdentity | None:
    return _identity.get()


@contextmanager
def _bind_cron_execution_identity(workspace, job_id):
    token = _identity.set(CronExecutionIdentity(workspace, job_id))
    try:
        yield
    finally:
        _identity.reset(token)
