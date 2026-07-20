"""Content-free JSON and SQL persistence for Ops Observer.

Storage backend is pluggable: SQLite (default, zero-config) or MySQL 5.7
(selected via the ``OPS_OBSERVER_DB_URL`` environment variable, e.g.
``mysql://user:pass@host:3306/dbname``). All SQL avoids CTEs, window
functions and dialect-specific features so the same queries run on both
SQLite and MySQL 5.7.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import sqlite3
import tempfile
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

logger = logging.getLogger(__name__)

_RUN_FIELDS = {
    "schema_version", "run_id", "agent_id", "session_key", "channel",
    "trigger_type", "started_at", "completed_at", "status", "duration_ms",
    "llm_call_count", "tool_call_count", "tool_error_count",
    "output_artifact_count", "error_category", "config_ref",
}
_ERRORS = {"success": None, "error": "execution_error", "timeout": "timeout", "cancelled": "cancelled"}
_PATTERNS = {key: re.compile(rf"^{key}-[A-Za-z0-9][A-Za-z0-9_.-]{{0,63}}$") for key in ("run", "agent", "config")}
_TOOL_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_CHANNEL_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_EVENT_TYPES = {
    "chat_opened", "delivery_viewed", "feedback_submitted",
    "page_viewed", "menu_clicked", "session_started",
    "message_sent", "file_uploaded",
}
_TOOL_STATES = {"success", "error", "denied", "interrupted", "cancelled"}
_LLM_STATES = {"success", "error", "cancelled"}


def _identifier(value: Any, namespace: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError("invalid identifier")
    if _PATTERNS[namespace].fullmatch(text):
        return text
    return f"{namespace}-{hashlib.sha256(f'{namespace}:{text}'.encode()).hexdigest()[:16]}"


def _hashed(value: Any, namespace: str) -> str:
    """Always-hash identifier for values that may carry user information."""
    text = str(value or "").strip()
    if not text:
        return ""
    return f"{namespace}-{hashlib.sha256(f'{namespace}:{text}'.encode()).hexdigest()[:16]}"


def _tool_name(value: Any) -> str:
    text = str(value or "").strip()
    if _TOOL_NAME_PATTERN.fullmatch(text):
        return text
    return _hashed(text, "tool") or "unknown"


def _channel(value: Any) -> str:
    text = str(value or "").strip().lower()
    if _CHANNEL_PATTERN.fullmatch(text):
        return text
    return "other" if text else ""


def _timestamp(value: Any) -> datetime:
    raw = str(value)
    parsed = datetime.fromisoformat(raw[:-1] + "+00:00" if raw.endswith("Z") else raw)
    if parsed.tzinfo is None:
        raise ValueError("invalid timestamp")
    return parsed.astimezone(UTC).replace(microsecond=0)


def _metric(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("invalid metric")
    return value


def normalize_summary(summary: dict[str, Any]) -> dict[str, Any]:
    if set(summary) != _RUN_FIELDS or summary.get("trigger_type") != "chat":
        raise ValueError("invalid summary fields")
    status = summary.get("status")
    if status not in _ERRORS or summary.get("error_category") != _ERRORS[status]:
        raise ValueError("invalid summary state")
    started, completed = _timestamp(summary["started_at"]), _timestamp(summary["completed_at"])
    metrics = ("duration_ms", "llm_call_count", "tool_call_count", "tool_error_count", "output_artifact_count")
    values = {key: _metric(summary[key]) for key in metrics}
    if completed < started:
        raise ValueError("invalid summary timing")
    return {
        "schema_version": 2, "run_id": _identifier(summary["run_id"], "run"),
        "agent_id": _identifier(summary["agent_id"], "agent"),
        "session_key": _hashed(summary.get("session_key"), "sess"),
        "channel": _channel(summary.get("channel")),
        "trigger_type": "chat",
        "started_at": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "completed_at": completed.strftime("%Y-%m-%dT%H:%M:%SZ"), "status": status,
        "duration_ms": values["duration_ms"], "llm_call_count": values["llm_call_count"],
        "tool_call_count": values["tool_call_count"], "tool_error_count": values["tool_error_count"],
        "output_artifact_count": values["output_artifact_count"],
        "error_category": _ERRORS[status], "config_ref": _identifier(summary["config_ref"], "config"),
    }


def normalize_tool_call(record: dict[str, Any]) -> dict[str, Any]:
    status = str(record.get("status", "")).lower()
    if status not in _TOOL_STATES:
        raise ValueError("invalid tool status")
    return {
        "run_id": _identifier(record["run_id"], "run"),
        "tool_seq": _metric(record["tool_seq"]),
        "tool_name": _tool_name(record.get("tool_name")),
        "status": status,
        "started_at": _timestamp(record["started_at"]).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "duration_ms": _metric(record["duration_ms"]),
    }


def normalize_llm_call(record: dict[str, Any]) -> dict[str, Any]:
    status = str(record.get("status", "")).lower()
    if status not in _LLM_STATES:
        raise ValueError("invalid llm status")
    ttft = record["ttft_ms"]
    if isinstance(ttft, bool) or not isinstance(ttft, int) or ttft < -1:
        raise ValueError("invalid ttft")
    return {
        "run_id": _identifier(record["run_id"], "run"),
        "call_seq": _metric(record["call_seq"]),
        "status": status,
        "started_at": _timestamp(record["started_at"]).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "duration_ms": _metric(record["duration_ms"]),
        "ttft_ms": ttft,
        "thinking_chunks": _metric(record["thinking_chunks"]),
        "text_chunks": _metric(record["text_chunks"]),
    }


# ── Database backends ─────────────────────────────────────────────────────

class _Database:
    """Minimal synchronous DB wrapper shared by both dialects."""

    param = "?"
    replace_prefix = "INSERT OR REPLACE"

    def execute(self, sql: str, params: tuple = ()) -> None:
        raise NotImplementedError

    def query(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    def create_tables(self) -> None:
        raise NotImplementedError

    def _existing_columns(self, table: str) -> set[str]:
        """Return column names for *table*. Both backends support PRAGMA or info_schema."""
        try:
            rows = self.query(f"PRAGMA table_info({table})")
            return {r["name"] for r in rows}
        except Exception:
            return set()

    def _add_column(self, table: str, column: str, definition: str) -> None:
        """Add a column to an existing table (ALTER TABLE ... ADD COLUMN)."""
        self.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _migrate_runs(self) -> None:
        """Add columns that were introduced in schema v2 if missing."""
        existing = self._existing_columns("runs")
        if not existing:
            return  # table doesn't exist yet; create_tables() will handle it
        new_cols = {
            "agent_id": "VARCHAR(191) NOT NULL DEFAULT ''",
            "session_key": "VARCHAR(191) NOT NULL DEFAULT ''",
            "channel": "VARCHAR(64) NOT NULL DEFAULT ''",
            "completed_at": "VARCHAR(32) NOT NULL DEFAULT ''",
            "llm_call_count": "INTEGER NOT NULL DEFAULT 0",
            "output_artifact_count": "INTEGER NOT NULL DEFAULT 0",
            "error_category": "VARCHAR(32)",
        }
        for col, defn in new_cols.items():
            if col not in existing:
                self._add_column("runs", col, defn)

    def _migrate_user_events(self) -> None:
        existing = self._existing_columns("user_events")
        if not existing:
            return
        if "event_key" not in existing:
            self._add_column("user_events", "event_key", "VARCHAR(191) NOT NULL DEFAULT ''")


class _SQLiteDatabase(_Database):
    param = "?"
    replace_prefix = "INSERT OR REPLACE"

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()

    def execute(self, sql: str, params: tuple = ()) -> None:
        with self._lock:
            self._conn.execute(sql, params)
            self._conn.commit()

    def query(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def create_tables(self) -> None:
        ddl = [
            """CREATE TABLE IF NOT EXISTS runs (
                run_id VARCHAR(191) PRIMARY KEY,
                agent_id VARCHAR(191) NOT NULL,
                session_key VARCHAR(191) NOT NULL DEFAULT '',
                channel VARCHAR(64) NOT NULL DEFAULT '',
                status VARCHAR(16) NOT NULL,
                started_at VARCHAR(32) NOT NULL,
                completed_at VARCHAR(32) NOT NULL,
                duration_ms INTEGER NOT NULL,
                llm_call_count INTEGER NOT NULL DEFAULT 0,
                tool_call_count INTEGER NOT NULL,
                tool_error_count INTEGER NOT NULL,
                output_artifact_count INTEGER NOT NULL DEFAULT 0,
                error_category VARCHAR(32)
            )""",
            """CREATE TABLE IF NOT EXISTS tool_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id VARCHAR(191) NOT NULL,
                tool_seq INTEGER NOT NULL,
                tool_name VARCHAR(191) NOT NULL,
                status VARCHAR(16) NOT NULL,
                started_at VARCHAR(32) NOT NULL,
                duration_ms INTEGER NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS llm_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id VARCHAR(191) NOT NULL,
                call_seq INTEGER NOT NULL,
                status VARCHAR(16) NOT NULL,
                started_at VARCHAR(32) NOT NULL,
                duration_ms INTEGER NOT NULL,
                ttft_ms INTEGER NOT NULL DEFAULT -1,
                thinking_chunks INTEGER NOT NULL DEFAULT 0,
                text_chunks INTEGER NOT NULL DEFAULT 0
            )""",
            """CREATE TABLE IF NOT EXISTS user_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type VARCHAR(64) NOT NULL,
                event_key VARCHAR(191) NOT NULL DEFAULT '',
                occurred_at VARCHAR(32) NOT NULL
            )""",
            "CREATE INDEX IF NOT EXISTS idx_runs_started ON runs (started_at)",
            "CREATE INDEX IF NOT EXISTS idx_tool_calls_run ON tool_calls (run_id)",
            "CREATE INDEX IF NOT EXISTS idx_tool_calls_started ON tool_calls (started_at)",
            "CREATE INDEX IF NOT EXISTS idx_llm_calls_run ON llm_calls (run_id)",
            "CREATE INDEX IF NOT EXISTS idx_user_events_occurred ON user_events (occurred_at)",
        ]
        with self._lock:
            for statement in ddl:
                self._conn.execute(statement)
            self._conn.commit()
        self._migrate_runs()
        self._migrate_user_events()


class _MySQLDatabase(_Database):
    """MySQL 5.7 backend via PyMySQL (lazy-imported).

    Indexed string columns use VARCHAR(191) so utf8mb4 indexes stay within
    the 767-byte InnoDB prefix limit on older MySQL 5.7 configurations.
    """

    param = "%s"
    replace_prefix = "REPLACE"

    def __init__(self, url: str) -> None:
        import pymysql  # lazy: only required when MySQL is configured

        parsed = urlparse(url)
        if parsed.scheme not in ("mysql", "mysql+pymysql"):
            raise ValueError(f"unsupported db url scheme: {parsed.scheme}")
        self._conn = pymysql.connect(
            host=parsed.hostname or "127.0.0.1",
            port=parsed.port or 3306,
            user=unquote(parsed.username or ""),
            password=unquote(parsed.password or ""),
            database=(parsed.path or "/").lstrip("/"),
            charset="utf8mb4",
            autocommit=False,
        )
        self._lock = threading.Lock()

    def _cursor(self):
        self._conn.ping(reconnect=True)
        return self._conn.cursor()

    def execute(self, sql: str, params: tuple = ()) -> None:
        with self._lock:
            cursor = self._cursor()
            try:
                cursor.execute(sql, params)
                self._conn.commit()
            finally:
                cursor.close()

    def query(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            cursor = self._cursor()
            try:
                cursor.execute(sql, params)
                columns = [col[0] for col in cursor.description]
                return [dict(zip(columns, row)) for row in cursor.fetchall()]
            finally:
                cursor.close()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def create_tables(self) -> None:
        ddl = [
            """CREATE TABLE IF NOT EXISTS runs (
                run_id VARCHAR(191) PRIMARY KEY,
                agent_id VARCHAR(191) NOT NULL,
                session_key VARCHAR(191) NOT NULL DEFAULT '',
                channel VARCHAR(64) NOT NULL DEFAULT '',
                status VARCHAR(16) NOT NULL,
                started_at VARCHAR(32) NOT NULL,
                completed_at VARCHAR(32) NOT NULL,
                duration_ms INT NOT NULL,
                llm_call_count INT NOT NULL DEFAULT 0,
                tool_call_count INT NOT NULL,
                tool_error_count INT NOT NULL,
                output_artifact_count INT NOT NULL DEFAULT 0,
                error_category VARCHAR(32),
                KEY idx_runs_started (started_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
            """CREATE TABLE IF NOT EXISTS tool_calls (
                id INT AUTO_INCREMENT PRIMARY KEY,
                run_id VARCHAR(191) NOT NULL,
                tool_seq INT NOT NULL,
                tool_name VARCHAR(191) NOT NULL,
                status VARCHAR(16) NOT NULL,
                started_at VARCHAR(32) NOT NULL,
                duration_ms INT NOT NULL,
                KEY idx_tool_calls_run (run_id),
                KEY idx_tool_calls_started (started_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
            """CREATE TABLE IF NOT EXISTS llm_calls (
                id INT AUTO_INCREMENT PRIMARY KEY,
                run_id VARCHAR(191) NOT NULL,
                call_seq INT NOT NULL,
                status VARCHAR(16) NOT NULL,
                started_at VARCHAR(32) NOT NULL,
                duration_ms INT NOT NULL,
                ttft_ms INT NOT NULL DEFAULT -1,
                thinking_chunks INT NOT NULL DEFAULT 0,
                text_chunks INT NOT NULL DEFAULT 0,
                KEY idx_llm_calls_run (run_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
            """CREATE TABLE IF NOT EXISTS user_events (
                id INT AUTO_INCREMENT PRIMARY KEY,
                event_type VARCHAR(64) NOT NULL,
                event_key VARCHAR(191) NOT NULL DEFAULT '',
                occurred_at VARCHAR(32) NOT NULL,
                KEY idx_user_events_occurred (occurred_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
        ]
        with self._lock:
            cursor = self._cursor()
            try:
                for statement in ddl:
                    cursor.execute(statement)
                self._conn.commit()
            finally:
                cursor.close()
        self._migrate_runs()
        self._migrate_user_events()

    def _existing_columns(self, table: str) -> set[str]:
        """MySQL uses information_schema, not PRAGMA."""
        try:
            rows = self.query(
                "SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
                (table,),
            )
            return {r["COLUMN_NAME"] for r in rows}
        except Exception:
            return set()

    def _add_column(self, table: str, column: str, definition: str) -> None:
        """MySQL ADD COLUMN definition uses MySQL types (INT, VARCHAR)."""
        mysql_defn = definition.replace("INTEGER", "INT")
        self.execute(f"ALTER TABLE {table} ADD COLUMN {column} {mysql_defn}")


def _open_database(root: Path) -> _Database:
    from .config import get_db_url
    url = get_db_url()
    if url.startswith("mysql"):
        logger.info("Ops Observer using MySQL backend")
        return _MySQLDatabase(url)
    return _SQLiteDatabase(root / "ops_observer" / "observer.sqlite3")


# ── Service ───────────────────────────────────────────────────────────────

class ObserverService:
    def __init__(self, root: Path) -> None:
        self._root = root
        self._queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue(maxsize=5000)
        self._worker: asyncio.Task | None = None
        self._db: _Database | None = None
        self._event_token = ""

    @classmethod
    def from_environment(cls, _config: dict[str, Any]) -> "ObserverService":
        from .config import get_working_dir
        return cls(get_working_dir())

    async def start(self) -> None:
        from .config import get_secret_dir
        try:
            self._db = _open_database(self._root)
            self._db.create_tables()
            secret_dir = get_secret_dir(self._root)
            secret_dir.mkdir(parents=True, exist_ok=True)
            token_file = secret_dir / "ops_observer_event.token"
            if token_file.exists():
                self._event_token = token_file.read_text(encoding="utf-8").strip()
            else:
                self._event_token = secrets.token_urlsafe(32)
                token_file.write_text(self._event_token, encoding="utf-8")
            self._worker = asyncio.create_task(self._drain())
            logger.info("Ops Observer service started (db=%s, root=%s)", type(self._db).__name__, self._root)
        except Exception:
            logger.error("Ops Observer service failed to start", exc_info=True)
            self._db = None

    def accepts_event_token(self, token: str | None) -> bool:
        return bool(self._event_token) and secrets.compare_digest(token or "", self._event_token)

    async def stop(self, **_kwargs: Any) -> None:
        if self._worker is not None:
            await self._queue.join()
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
            self._worker = None
        if self._db is not None:
            self._db.close()
            self._db = None

    # ── Write path (enqueue, drained by background worker) ──

    def enqueue_run_summary(self, summary: dict[str, Any]) -> None:
        if self._db is None:
            return
        try:
            self._queue.put_nowait(("run", normalize_summary(summary)))
        except (asyncio.QueueFull, ValueError):
            return

    def enqueue_tool_call(self, record: dict[str, Any]) -> None:
        if self._db is None:
            return
        try:
            self._queue.put_nowait(("tool_call", normalize_tool_call(record)))
        except (asyncio.QueueFull, ValueError):
            return

    def enqueue_llm_call(self, record: dict[str, Any]) -> None:
        if self._db is None:
            return
        try:
            self._queue.put_nowait(("llm_call", normalize_llm_call(record)))
        except (asyncio.QueueFull, ValueError):
            return

    def enqueue_user_event(self, event_type: str, occurred_at: str, event_key: str = "") -> bool:
        if event_type not in _EVENT_TYPES:
            return False
        try:
            _timestamp(occurred_at)
            key = _hashed(event_key, "key") if event_key else ""
            self._queue.put_nowait(("user_event", {
                "event_type": event_type, "event_key": key, "occurred_at": occurred_at,
            }))
            return True
        except (asyncio.QueueFull, ValueError):
            return False

    async def _drain(self) -> None:
        while True:
            kind, payload = await self._queue.get()
            try:
                if kind == "run":
                    await asyncio.to_thread(self._write_summary, payload)
                elif kind == "tool_call":
                    await asyncio.to_thread(self._write_tool_call, payload)
                elif kind == "llm_call":
                    await asyncio.to_thread(self._write_llm_call, payload)
                else:
                    await asyncio.to_thread(self._write_user_event, payload)
            except Exception:
                logger.debug("ops observer write failed", exc_info=True)
            finally:
                self._queue.task_done()

    def _write_summary(self, summary: dict[str, Any]) -> None:
        assert self._db is not None
        directory = self._root / "run_summaries"
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory, prefix=".run-summary-", suffix=".tmp", delete=False) as handle:
            json.dump(summary, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            temporary = Path(handle.name)
        temporary.replace(directory / f"{summary['run_id']}.json")
        p = self._db.param
        self._db.execute(
            f"{self._db.replace_prefix} INTO runs (run_id, agent_id, session_key, channel, status,"
            f" started_at, completed_at, duration_ms, llm_call_count, tool_call_count,"
            f" tool_error_count, output_artifact_count, error_category) VALUES"
            f" ({p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p})",
            (summary["run_id"], summary["agent_id"], summary["session_key"], summary["channel"],
             summary["status"], summary["started_at"], summary["completed_at"], summary["duration_ms"],
             summary["llm_call_count"], summary["tool_call_count"], summary["tool_error_count"],
             summary["output_artifact_count"], summary["error_category"]),
        )

    def _write_tool_call(self, record: dict[str, Any]) -> None:
        assert self._db is not None
        p = self._db.param
        self._db.execute(
            f"INSERT INTO tool_calls (run_id, tool_seq, tool_name, status, started_at, duration_ms)"
            f" VALUES ({p}, {p}, {p}, {p}, {p}, {p})",
            (record["run_id"], record["tool_seq"], record["tool_name"], record["status"],
             record["started_at"], record["duration_ms"]),
        )

    def _write_llm_call(self, record: dict[str, Any]) -> None:
        assert self._db is not None
        p = self._db.param
        self._db.execute(
            f"INSERT INTO llm_calls (run_id, call_seq, status, started_at, duration_ms, ttft_ms,"
            f" thinking_chunks, text_chunks) VALUES ({p}, {p}, {p}, {p}, {p}, {p}, {p}, {p})",
            (record["run_id"], record["call_seq"], record["status"], record["started_at"],
             record["duration_ms"], record["ttft_ms"], record["thinking_chunks"], record["text_chunks"]),
        )

    def _write_user_event(self, event: dict[str, str]) -> None:
        assert self._db is not None
        p = self._db.param
        self._db.execute(
            f"INSERT INTO user_events (event_type, event_key, occurred_at) VALUES ({p}, {p}, {p})",
            (event["event_type"], event["event_key"], event["occurred_at"]),
        )

    # ── Read path (stats queries, run in worker thread) ──

    def _ready(self) -> bool:
        if self._db is None:
            logger.warning("Ops Observer stats requested but service is not ready (db=None)")
            return False
        return True

    @staticmethod
    def _since(hours: int) -> str:
        hours = max(1, min(int(hours), 720))
        return (datetime.now(UTC) - timedelta(hours=hours)).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")

    async def stats_overview(self, hours: int = 24) -> dict[str, Any]:
        if not self._ready():
            return {"hours": hours, "total_runs": 0, "success_runs": 0, "success_rate": None,
                    "avg_duration_ms": None, "total_tool_calls": 0, "total_tool_errors": 0,
                    "tool_error_rate": None, "total_llm_calls": 0, "active_agents": 0,
                    "total_user_events": 0}
        since = self._since(hours)
        p = self._db.param
        rows = await asyncio.to_thread(self._db.query, f"""
            SELECT COUNT(*) AS total_runs,
                   SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS success_runs,
                   AVG(duration_ms) AS avg_duration_ms,
                   SUM(tool_call_count) AS total_tool_calls,
                   SUM(tool_error_count) AS total_tool_errors,
                   SUM(llm_call_count) AS total_llm_calls,
                   COUNT(DISTINCT agent_id) AS active_agents
            FROM runs WHERE started_at >= {p}
        """, (since,))
        row = rows[0] if rows else {}
        total = row.get("total_runs") or 0
        success = row.get("success_runs") or 0
        tool_calls = row.get("total_tool_calls") or 0
        tool_errors = row.get("total_tool_errors") or 0
        event_rows = await asyncio.to_thread(self._db.query, f"""
            SELECT COUNT(*) AS total_events FROM user_events WHERE occurred_at >= {p}
        """, (since,))
        return {
            "hours": hours,
            "total_runs": total,
            "success_runs": success,
            "success_rate": round(success / total, 4) if total else None,
            "avg_duration_ms": round(row["avg_duration_ms"], 1) if row.get("avg_duration_ms") is not None else None,
            "total_tool_calls": tool_calls,
            "total_tool_errors": tool_errors,
            "tool_error_rate": round(tool_errors / tool_calls, 4) if tool_calls else None,
            "total_llm_calls": row.get("total_llm_calls") or 0,
            "active_agents": row.get("active_agents") or 0,
            "total_user_events": (event_rows[0].get("total_events") or 0) if event_rows else 0,
        }

    async def stats_timeseries(self, hours: int = 24) -> dict[str, Any]:
        if not self._ready():
            return {"hours": hours, "buckets": []}
        since = self._since(hours)
        p = self._db.param
        rows = await asyncio.to_thread(self._db.query, f"""
            SELECT SUBSTR(started_at, 1, 13) AS bucket,
                   COUNT(*) AS runs,
                   SUM(CASE WHEN status != 'success' THEN 1 ELSE 0 END) AS errors,
                   AVG(duration_ms) AS avg_duration_ms
            FROM runs WHERE started_at >= {p}
            GROUP BY SUBSTR(started_at, 1, 13) ORDER BY bucket
        """, (since,))
        return {
            "hours": hours,
            "buckets": [
                {
                    "bucket": row["bucket"],
                    "runs": row["runs"],
                    "errors": row["errors"] or 0,
                    "avg_duration_ms": round(row["avg_duration_ms"], 1) if row.get("avg_duration_ms") is not None else None,
                }
                for row in rows
            ],
        }

    async def stats_tools(self, hours: int = 24, limit: int = 10) -> dict[str, Any]:
        if not self._ready():
            return {"hours": hours, "tools": []}
        since = self._since(hours)
        limit = max(1, min(int(limit), 100))
        p = self._db.param
        rows = await asyncio.to_thread(self._db.query, f"""
            SELECT tool_name, COUNT(*) AS calls,
                   SUM(CASE WHEN status != 'success' THEN 1 ELSE 0 END) AS errors,
                   AVG(duration_ms) AS avg_duration_ms
            FROM tool_calls WHERE started_at >= {p}
            GROUP BY tool_name ORDER BY calls DESC LIMIT {int(limit)}
        """, (since,))
        return {
            "hours": hours,
            "tools": [
                {
                    "tool_name": row["tool_name"],
                    "calls": row["calls"],
                    "errors": row["errors"] or 0,
                    "avg_duration_ms": round(row["avg_duration_ms"], 1) if row.get("avg_duration_ms") is not None else None,
                }
                for row in rows
            ],
        }

    async def stats_agents(self, hours: int = 24, limit: int = 10) -> dict[str, Any]:
        if not self._ready():
            return {"hours": hours, "agents": []}
        since = self._since(hours)
        limit = max(1, min(int(limit), 100))
        p = self._db.param
        rows = await asyncio.to_thread(self._db.query, f"""
            SELECT agent_id, COUNT(*) AS runs,
                   SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS success_runs,
                   AVG(duration_ms) AS avg_duration_ms,
                   SUM(tool_call_count) AS tool_calls
            FROM runs WHERE started_at >= {p}
            GROUP BY agent_id ORDER BY runs DESC LIMIT {int(limit)}
        """, (since,))
        return {
            "hours": hours,
            "agents": [
                {
                    "agent_id": row["agent_id"],
                    "runs": row["runs"],
                    "success_runs": row["success_runs"] or 0,
                    "avg_duration_ms": round(row["avg_duration_ms"], 1) if row.get("avg_duration_ms") is not None else None,
                    "tool_calls": row["tool_calls"] or 0,
                }
                for row in rows
            ],
        }

    async def stats_events(self, hours: int = 24) -> dict[str, Any]:
        if not self._ready():
            return {"hours": hours, "events": []}
        since = self._since(hours)
        p = self._db.param
        rows = await asyncio.to_thread(self._db.query, f"""
            SELECT event_type, COUNT(*) AS count
            FROM user_events WHERE occurred_at >= {p}
            GROUP BY event_type ORDER BY count DESC
        """, (since,))
        return {"hours": hours, "events": [{"event_type": r["event_type"], "count": r["count"]} for r in rows]}

    async def stats_llm(self, hours: int = 24) -> dict[str, Any]:
        if not self._ready():
            return {"hours": hours, "calls": 0, "errors": 0, "avg_duration_ms": None, "avg_ttft_ms": None}
        since = self._since(hours)
        p = self._db.param
        rows = await asyncio.to_thread(self._db.query, f"""
            SELECT COUNT(*) AS calls,
                   SUM(CASE WHEN status != 'success' THEN 1 ELSE 0 END) AS errors,
                   AVG(duration_ms) AS avg_duration_ms,
                   AVG(CASE WHEN ttft_ms >= 0 THEN ttft_ms END) AS avg_ttft_ms
            FROM llm_calls WHERE started_at >= {p}
        """, (since,))
        row = rows[0] if rows else {}
        return {
            "hours": hours,
            "calls": row.get("calls") or 0,
            "errors": row.get("errors") or 0,
            "avg_duration_ms": round(row["avg_duration_ms"], 1) if row.get("avg_duration_ms") is not None else None,
            "avg_ttft_ms": round(row["avg_ttft_ms"], 1) if row.get("avg_ttft_ms") is not None else None,
        }

    async def recent_runs(self, limit: int = 20) -> dict[str, Any]:
        if not self._ready():
            return {"runs": []}
        limit = max(1, min(int(limit), 100))
        rows = await asyncio.to_thread(self._db.query, f"""
            SELECT run_id, agent_id, channel, status, started_at, duration_ms,
                   llm_call_count, tool_call_count, tool_error_count
            FROM runs ORDER BY started_at DESC LIMIT {int(limit)}
        """)
        return {"runs": rows}
