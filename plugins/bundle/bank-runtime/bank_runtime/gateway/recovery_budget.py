"""Bounded, request-local recovery for individual controlled tool operations.

This ledger authorizes no execution: every candidate still needs a fresh
Runtime preflight and Tool Guard decision. It stores hashes, never file bodies.
"""
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .protocol import canonical_payload_hash


def recovery_operation_key(name: str, payload: Any) -> tuple[str, str]:
    if not isinstance(payload, Mapping):
        return name, "malformed"
    # Content is repairable; identities, formats and input sources distinguish
    # independent deliverables. A task cap bounds attempts to rotate identities.
    identity = {key: payload[key] for key in (
        "artifact_type", "target_format", "format", "output_name", "title",
        "source_id", "source_generated_file_id", "source_refs", "file_id",
        "file_ref", "document_ref", "chart_id", "version_id", "template_id",
        "template_version_id", "purpose",
    ) if key in payload}
    return name, canonical_payload_hash(identity)


@dataclass
class _OperationRecovery:
    failures: int = 0
    input_failures: int = 0
    attempts: Counter = field(default_factory=Counter)
    terminal_reason: str = ""
    latest_error: tuple[str, str] = ("", "")


class OperationRecoveryBudget:
    """Two equal failures, four repairs per operation, sixteen per task.

    Changed validated input/diagnostics may justify another bounded attempt.
    Returning to a failed input never forgets its prior attempts. Unknown side
    effects and an explicitly exhausted renderer cannot be retried via repairs.
    The existing task deadline remains the authoritative wall-clock limit.
    """

    def __init__(self, *, repeat_limit=2, operation_limit=4, task_limit=16):
        if min(repeat_limit, operation_limit, task_limit) < 1:
            raise ValueError("Recovery limits must be positive")
        self.repeat_limit = repeat_limit
        self.operation_limit = operation_limit
        self.task_limit = task_limit
        self.total_failures = 0
        self._operations: dict[tuple[str, str], _OperationRecovery] = {}

    @property
    def task_exhausted(self) -> bool:
        return self.total_failures >= self.task_limit

    @property
    def pending_count(self) -> int:
        return sum(item.failures for item in self._operations.values())

    @property
    def pending_input_failures(self) -> int:
        # Unknown execution and exhausted rendering are not argument errors.
        return sum(item.input_failures for item in self._operations.values() if not item.terminal_reason)

    def fail(self, name, payload, error_code, *, diagnostic="",
             execution_state="not_started", terminal_reason="") -> None:
        key = recovery_operation_key(name, payload)
        item = self._operations.setdefault(key, _OperationRecovery())
        item.failures += 1
        if error_code in {"INVALID_JSON", "MALFORMED_ARGUMENTS", "INVALID_REQUEST", "BAD_REQUEST", "ARTIFACT_VALIDATION_FAILED"}:
            item.input_failures += 1
        self.total_failures += 1
        error = (str(error_code), canonical_payload_hash(str(diagnostic)))
        item.latest_error = error
        item.attempts[(canonical_payload_hash(payload), *error)] += 1
        if execution_state in {"unknown", "execution_unknown", "executing", "pending"}:
            item.terminal_reason = "execution_unknown"
        elif terminal_reason:
            item.terminal_reason = str(terminal_reason)

    def blocked_reason(self, name, payload) -> str:
        if self.task_exhausted:
            return "task_recovery_budget_exhausted"
        item = self._operations.get(recovery_operation_key(name, payload))
        if item is None:
            return ""
        if item.terminal_reason:
            return item.terminal_reason
        if item.failures >= self.operation_limit:
            return "operation_recovery_budget_exhausted"
        payload_hash = canonical_payload_hash(payload)
        # A new error class/diagnostic is progress even for the same input.
        if item.attempts[(payload_hash, *item.latest_error)] >= self.repeat_limit:
            return "unchanged_input_recovery_exhausted"
        # Cycling to an older rejected input cannot reset its exhausted budget.
        if any(digest == payload_hash and count >= self.repeat_limit
               for (digest, *_), count in item.attempts.items()):
            return "unchanged_input_recovery_exhausted"
        return ""

    def succeed(self, name, payload) -> None:
        self._operations.pop(recovery_operation_key(name, payload), None)
        # Invalid JSON never started a file operation. A verified success from
        # this tool also proves its arguments have crossed that syntax boundary.
        self._operations.pop((name, "malformed"), None)
