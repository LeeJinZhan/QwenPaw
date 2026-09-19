"""Bounded, content-free outbox for Runtime result callbacks."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import time
from typing import Any, Awaitable, Callable

OUTBOX_STATUSES = frozenset(
    {
        "completed",
        "failed",
        "cancelled",
        "not_started_cancelled",
        "execution_interrupted",
        "execution_unknown",
    }
)


class GatewayResultOutbox:
    """Persist callback metadata only; never retry tool execution."""

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        max_attempts: int = 8,
    ) -> None:
        self.root = (
            Path(
                root
                or os.environ.get("QWENPAW_RUNTIME_TOOL_OUTBOX_ROOT")
                or "/tmp/qwenpaw-runtime-tool-outbox"
            )
            .expanduser()
            .resolve()
        )
        self.max_attempts = max(1, min(int(max_attempts), 32))
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)

    def enqueue(
        self,
        *,
        task_id: str,
        tool_call_id: str,
        status: str,
        duration_ms: int,
        error_code: str = "",
        protocol: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        normalized_status = str(status or "").strip()
        if normalized_status not in OUTBOX_STATUSES:
            raise ValueError("unsupported tool outbox status")
        safe_task = _safe_id(task_id)
        safe_call = _safe_id(tool_call_id)
        timestamp = time.time()
        record = {
            "task_id": safe_task,
            "tool_call_id": safe_call,
            "status": normalized_status,
            "duration_ms": max(int(duration_ms), 0),
            "error_code": str(error_code or "")[:128],
            "protocol": {
                str(key): str(value)[:256] for key, value in (protocol or {}).items()
            },
            "attempts": 0,
            "created_at_epoch": timestamp,
            "updated_at_epoch": timestamp,
        }
        self._atomic_write(self._path(safe_task, safe_call), record)
        return record

    def pending(self, task_id: str) -> list[dict[str, Any]]:
        safe_task = _safe_id(task_id)
        records: list[dict[str, Any]] = []
        for path in sorted(self.root.glob(f"{safe_task}--*.json")):
            record = self._read(path)
            if record and record.get("task_id") == safe_task:
                records.append(record)
        return records

    async def flush(
        self,
        task_id: str,
        sender: Callable[[dict[str, Any]], Awaitable[Any]],
    ) -> dict[str, int]:
        delivered = pending = exhausted = 0
        for record in self.pending(task_id):
            path = self._path(record["task_id"], record["tool_call_id"])
            attempts = int(record.get("attempts") or 0)
            if attempts >= self.max_attempts:
                exhausted += 1
                continue
            try:
                await sender(dict(record))
            except Exception:  # callback failures retain safe metadata
                record["attempts"] = attempts + 1
                record["updated_at_epoch"] = time.time()
                self._atomic_write(path, record)
                pending += 1
            else:
                path.unlink(missing_ok=True)
                delivered += 1
        return {
            "delivered": delivered,
            "pending": pending,
            "exhausted": exhausted,
        }

    def remove(self, task_id: str, tool_call_id: str) -> None:
        self._path(_safe_id(task_id), _safe_id(tool_call_id)).unlink(missing_ok=True)

    def _path(self, task_id: str, tool_call_id: str) -> Path:
        digest = hashlib.sha256(tool_call_id.encode("utf-8")).hexdigest()[:24]
        path = (self.root / f"{task_id}--{digest}.json").resolve()
        if path.parent != self.root:
            raise ValueError("tool outbox path escaped root")
        return path

    @staticmethod
    def _read(path: Path) -> dict[str, Any] | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _atomic_write(path: Path, record: dict[str, Any]) -> None:
        temporary = path.with_suffix(f".tmp-{os.getpid()}-{id(record)}")
        temporary.write_text(
            json.dumps(record, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)


def _safe_id(value: str) -> str:
    normalized = str(value or "").strip()
    if (
        not normalized
        or len(normalized) > 160
        or any(
            char
            not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
            for char in normalized
        )
    ):
        raise ValueError("invalid tool outbox identifier")
    return normalized


__all__ = ["GatewayResultOutbox", "OUTBOX_STATUSES"]
