# -*- coding: utf-8 -*-
"""Runtime-authorized object reader for sandbox attachments."""
from __future__ import annotations

import asyncio
import atexit
import hashlib
import json
import logging
import os
import shutil
import threading
import time
import uuid
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin

from ...config.runtime_endpoint import resolve_runtime_base_url

if os.environ.get("QWENPAW_SANDBOX_DAEMON_MODE") == "1":
    def get_current_runtime_tool_gateway() -> None:
        return None
else:
    from ...config.context import get_current_runtime_tool_gateway


logger = logging.getLogger(__name__)

_TASK_MARKER_FILENAME = ".task-marker.json"
_DOWNLOAD_CHUNK_BYTES = 1024 * 1024
_ATOMIC_TEMP_SUFFIX_BYTES = len(".part.") + 32
_MAX_ORIGINAL_NAME_BYTES = 255 - _ATOMIC_TEMP_SUFFIX_BYTES
_MAX_SAFE_EXTENSION_CHARS = 16
_run_in_thread = asyncio.to_thread


@dataclass(frozen=True)
class SandboxedObjectContent:
    """Content returned after Runtime authorizes one sandbox file read."""

    content: bytes
    content_type: str
    size_bytes: int


@dataclass(frozen=True)
class PreparedSandboxFile:
    """Task-local prepared attachment path."""

    file_id: str
    local_path: Path
    content_type: str
    size_bytes: int
    original_name: str
    expires_at: str


@dataclass(frozen=True)
class TaskMarker:
    """Task-local cache lifetime marker."""

    task_id: str
    sandbox_context_id: str
    created_at_epoch: float
    expires_at_epoch: float


class TaskIoReservation:
    """One in-flight task IO reference held across thread submission."""

    def __init__(self, cache: "TaskAttachmentCache", task_id: str) -> None:
        self._cache = cache
        self.task_id = task_id
        self._released = False
        self._release_lock = threading.Lock()

    def release(self) -> None:
        """Release this reservation exactly once."""
        with self._release_lock:
            if self._released:
                return
            self._released = True
        self._cache._release_task_io(self.task_id)

    def __enter__(self) -> "TaskIoReservation":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.release()


class RuntimeAttachmentPreparationError(RuntimeError):
    """Typed failure while preparing an explicit Runtime attachment."""

    def __init__(self, file_id: str, reason_code: str) -> None:
        self.file_id = str(file_id or "").strip()
        self.reason_code = (
            str(reason_code or "").strip() or "ATTACHMENT_READ_FAILED"
        )
        super().__init__(
            f"Runtime attachment preparation failed: {self.reason_code}",
        )


class TaskAttachmentCache:
    """Prepare Runtime-authorized attachments in task-local directories."""

    def __init__(
        self,
        *,
        root: str | Path | None = None,
        ttl_seconds: int | float | None = None,
    ) -> None:
        self.root = Path(
            root
            or os.environ.get("QWENPAW_TASK_FILE_ROOT")
            or "/tmp/qwenpaw-runtime-task-files",
        ).expanduser().resolve()
        self.ttl_seconds = _task_file_ttl_seconds(ttl_seconds)
        self.max_file_count = _task_file_max_count()
        self.max_total_bytes = _task_file_max_total_bytes()
        self._prepared: dict[tuple[str, str, str], PreparedSandboxFile] = {}
        self._quota_committed: dict[tuple[str, str, str], int] = {}
        self._quota_reservations: dict[tuple[str, str, str], int] = {}
        self._markers: dict[str, TaskMarker] = {}
        self._lock = threading.RLock()
        self._download_locks: dict[tuple[str, str, str], threading.Lock] = {}
        self._batch_locks: dict[tuple[str, str], threading.Lock] = {}
        self._batch_lock_users: dict[tuple[str, str], int] = {}
        self._cleanup_locks: dict[str, threading.Lock] = {}
        self._cleanup_users: dict[str, int] = {}
        self._io_reservations: dict[str, int] = {}
        self._batch_condition = threading.Condition(self._lock)
        self._cleaning_tasks: set[str] = set()
        self._sweeper_control_lock = threading.Lock()
        self._sweeper_stop = threading.Event()
        self._sweeper_thread: threading.Thread | None = None
        self._sweep_interval_seconds = _task_file_sweep_interval_seconds()

    def start_sweeper(
        self,
        *,
        interval_seconds: int | float | None = None,
    ) -> None:
        """Start the optional daemon TTL sweep loop for this cache."""
        interval = _task_file_sweep_interval_seconds(interval_seconds)
        with self._sweeper_control_lock:
            if (
                self._sweeper_thread is not None
                and self._sweeper_thread.is_alive()
            ):
                return
            self._sweep_interval_seconds = interval
            self._sweeper_stop.clear()
            thread = threading.Thread(
                target=self._run_sweeper,
                name="QwenPawTaskAttachmentSweeper",
                daemon=True,
            )
            self._sweeper_thread = thread
            thread.start()

    def stop_sweeper(self, *, timeout: float | None = None) -> None:
        """Stop the optional daemon TTL sweep loop and wait for exit."""
        with self._sweeper_control_lock:
            thread = self._sweeper_thread
            self._sweeper_stop.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        with self._sweeper_control_lock:
            if self._sweeper_thread is thread and (
                thread is None or not thread.is_alive()
            ):
                self._sweeper_thread = None

    def _run_sweeper(self) -> None:
        while not self._sweeper_stop.wait(self._sweep_interval_seconds):
            try:
                self.sweep_expired()
            except Exception:
                logger.exception("Runtime task attachment TTL sweep failed")

    def reserve_task_io(
        self,
        task_id: str,
        *,
        file_id: str = "",
    ) -> TaskIoReservation:
        """Reserve one task IO operation before scheduling or cache sweep."""
        safe_task_id = _safe_cache_id(task_id, "task_id")
        with self._batch_condition:
            if safe_task_id in self._cleaning_tasks:
                raise RuntimeAttachmentPreparationError(
                    file_id,
                    "ATTACHMENT_CACHE_UNAVAILABLE",
                )
            self._io_reservations[safe_task_id] = (
                self._io_reservations.get(safe_task_id, 0) + 1
            )
            self._batch_condition.notify_all()
        return TaskIoReservation(self, safe_task_id)

    def _release_task_io(self, task_id: str) -> None:
        with self._batch_condition:
            remaining = self._io_reservations.get(task_id, 0) - 1
            if remaining > 0:
                self._io_reservations[task_id] = remaining
            else:
                self._io_reservations.pop(task_id, None)
            self._batch_condition.notify_all()

    def prepare_file(
        self,
        file_id: str,
        sandbox_context: dict[str, Any],
        *,
        client: "SandboxedOssClient | None" = None,
    ) -> PreparedSandboxFile:
        """Authorize, download once, and return the task-local file path."""
        reader = client or SandboxedOssClient()
        safe_file_id = _safe_cache_id(file_id, "file_id")
        if not isinstance(sandbox_context, dict):
            raise RuntimeError("Sandbox context is invalid.")
        task_id = _safe_cache_id(sandbox_context.get("task_id"), "task_id")
        with self.reserve_task_io(task_id, file_id=safe_file_id):
            self.sweep_expired(exclude_task_ids={task_id})
            return self._prepare_with_locator_provider(
                safe_file_id,
                sandbox_context,
                reader,
                lambda: reader.authorize_file(safe_file_id, sandbox_context),
            )

    def prepare_task_workspace(
        self,
        sandbox_context: dict[str, Any],
    ) -> Path:
        """Create and return the private root for one Runtime task request."""
        if not isinstance(sandbox_context, dict):
            raise RuntimeError("Sandbox context is invalid.")
        task_id = _safe_cache_id(sandbox_context.get("task_id"), "task_id")
        context_id = _sandbox_cache_context_id(sandbox_context)
        with self.reserve_task_io(task_id):
            self.sweep_expired(exclude_task_ids={task_id})
            self._ensure_task_marker(task_id, context_id)
            task_root = self._task_root(task_id).resolve(strict=False)
            for directory in (task_root / "scratch", task_root / "output"):
                self._ensure_private_dir(directory)
            self._assert_cache_path(task_root)
            return task_root

    def prepare_files(
        self,
        file_ids: list[str],
        sandbox_context: dict[str, Any],
        *,
        client: "SandboxedOssClient | None" = None,
        selection_records: list[dict[str, str]] | None = None,
    ) -> list[PreparedSandboxFile]:
        """Batch-authorize cache misses and prepare files in manifest order."""
        fallback_file_id = ""
        try:
            if not isinstance(file_ids, list):
                raise RuntimeError("Sandbox attachment file_ids are invalid.")
            if file_ids:
                fallback_file_id = str(file_ids[0] or "").strip()
            ordered = list(
                dict.fromkeys(
                    _safe_cache_id(file_id, "file_id")
                    for file_id in file_ids
                ),
            )
        except (RuntimeError, TypeError, ValueError) as exc:
            raise RuntimeAttachmentPreparationError(
                fallback_file_id,
                "ATTACHMENT_INPUT_INVALID",
            ) from exc
        if not ordered:
            return []
        if not isinstance(sandbox_context, dict):
            raise RuntimeAttachmentPreparationError(
                ordered[0],
                "SANDBOX_CONTEXT_INVALID",
            )
        try:
            task_id = _safe_cache_id(
                sandbox_context.get("task_id"),
                "task_id",
            )
            context_id = _sandbox_cache_context_id(sandbox_context)
        except RuntimeError as exc:
            raise RuntimeAttachmentPreparationError(
                ordered[0],
                "SANDBOX_CONTEXT_INVALID",
            ) from exc
        with self.reserve_task_io(task_id, file_id=ordered[0]):
            self.sweep_expired(exclude_task_ids={task_id})
            batch_key = (task_id, context_id)
            batch_lock = self._register_batch_lock_user(
                batch_key,
                ordered[0],
            )
            batch_lock.acquire()
            try:
                return self._prepare_files_with_batch_lock(
                    ordered,
                    sandbox_context,
                    task_id=task_id,
                    context_id=context_id,
                    client=client,
                    selection_records=selection_records,
                )
            finally:
                batch_lock.release()
                self._unregister_batch_lock_user(batch_key)

    def _register_batch_lock_user(
        self,
        batch_key: tuple[str, str],
        file_id: str,
    ) -> threading.Lock:
        with self._lock:
            if batch_key[0] in self._cleaning_tasks:
                raise RuntimeAttachmentPreparationError(
                    file_id,
                    "ATTACHMENT_CACHE_UNAVAILABLE",
                )
            batch_lock = self._batch_locks.setdefault(
                batch_key,
                threading.Lock(),
            )
            self._batch_lock_users[batch_key] = (
                self._batch_lock_users.get(batch_key, 0) + 1
            )
            return batch_lock

    def _unregister_batch_lock_user(
        self,
        batch_key: tuple[str, str],
    ) -> None:
        with self._batch_condition:
            remaining = self._batch_lock_users.get(batch_key, 0) - 1
            if remaining > 0:
                self._batch_lock_users[batch_key] = remaining
            else:
                self._batch_lock_users.pop(batch_key, None)
            self._batch_condition.notify_all()

    def _prepare_files_with_batch_lock(
        self,
        ordered: list[str],
        sandbox_context: dict[str, Any],
        *,
        task_id: str,
        context_id: str,
        client: "SandboxedOssClient | None",
        selection_records: list[dict[str, str]] | None,
    ) -> list[PreparedSandboxFile]:
        with self._lock:
            if task_id in self._cleaning_tasks:
                raise RuntimeAttachmentPreparationError(
                    ordered[0],
                    "ATTACHMENT_CACHE_UNAVAILABLE",
                )
            cached = {
                file_id: prepared
                for file_id in ordered
                if (
                    (prepared := self._prepared.get(
                        (task_id, context_id, file_id),
                    ))
                    is not None
                    and prepared.local_path.is_file()
                )
            }
        misses = [file_id for file_id in ordered if file_id not in cached]
        if not misses:
            return [cached[file_id] for file_id in ordered]

        reader = client or SandboxedOssClient()
        try:
            selected_misses = [
                item
                for item in (selection_records or [])
                if str(item.get("file_id") or "").strip() in misses
            ]
            if selected_misses:
                authorization = reader.authorize_files(
                    misses,
                    sandbox_context,
                    selection_records=selected_misses,
                )
            else:
                authorization = reader.authorize_files(
                    misses,
                    sandbox_context,
                )
        except RuntimeAttachmentPreparationError:
            raise
        except Exception as exc:
            raise RuntimeAttachmentPreparationError(
                misses[0],
                "ATTACHMENT_AUTHORIZATION_FAILED",
            ) from exc
        if not isinstance(authorization, dict):
            raise RuntimeAttachmentPreparationError(
                misses[0],
                "ATTACHMENT_AUTHORIZATION_FAILED",
            )
        authorized_items = authorization.get("authorized", [])
        denied_items = authorization.get("denied", [])
        if not isinstance(authorized_items, list) or not isinstance(
            denied_items,
            list,
        ):
            raise RuntimeAttachmentPreparationError(
                misses[0],
                "ATTACHMENT_AUTHORIZATION_FAILED",
            )

        denied = {
            str(item.get("file_id", "")).strip(): str(
                item.get("reason_code") or "ATTACHMENT_READ_FAILED",
            ).strip()
            for item in denied_items
            if isinstance(item, dict)
        }
        for file_id in misses:
            if file_id in denied:
                raise RuntimeAttachmentPreparationError(
                    file_id,
                    denied[file_id],
                )

        locators: dict[str, dict[str, Any]] = {}
        for item in authorized_items:
            if not isinstance(item, dict):
                continue
            try:
                authorized_file_id = _safe_cache_id(
                    item.get("file_id"),
                    "file_id",
                )
            except RuntimeError:
                continue
            if authorized_file_id in misses:
                locators[authorized_file_id] = item
        for file_id in misses:
            if file_id not in locators:
                raise RuntimeAttachmentPreparationError(
                    file_id,
                    "ATTACHMENT_AUTHORIZATION_MISSING",
                )

        prepared_by_id = dict(cached)
        for file_id in misses:
            try:
                prepared_by_id[file_id] = self._prepare_authorized_file_no_sweep(
                    locators[file_id],
                    sandbox_context,
                    client=reader,
                )
            except RuntimeAttachmentPreparationError:
                raise
            except Exception as exc:
                raise RuntimeAttachmentPreparationError(
                    file_id,
                    "ATTACHMENT_READ_FAILED",
                ) from exc
        return [prepared_by_id[file_id] for file_id in ordered]

    def prepare_authorized_file(
        self,
        locator: dict[str, Any],
        sandbox_context: dict[str, Any],
        *,
        client: "SandboxedOssClient | None" = None,
    ) -> PreparedSandboxFile:
        """Download one already-authorized locator without re-authorizing."""
        if not isinstance(locator, dict) or not isinstance(
            sandbox_context,
            dict,
        ):
            raise RuntimeError("Runtime returned invalid attachment locator.")
        file_id = _safe_cache_id(locator.get("file_id"), "file_id")
        task_id = _safe_cache_id(sandbox_context.get("task_id"), "task_id")
        with self.reserve_task_io(task_id, file_id=file_id):
            self.sweep_expired(exclude_task_ids={task_id})
            return self._prepare_authorized_file_no_sweep(
                locator,
                sandbox_context,
                client=client,
            )

    def _prepare_authorized_file_no_sweep(
        self,
        locator: dict[str, Any],
        sandbox_context: dict[str, Any],
        *,
        client: "SandboxedOssClient | None" = None,
    ) -> PreparedSandboxFile:
        if not isinstance(locator, dict):
            raise RuntimeError("Runtime returned invalid attachment locator.")
        safe_file_id = _safe_cache_id(locator.get("file_id"), "file_id")
        reader = client or SandboxedOssClient()
        return self._prepare_with_locator_provider(
            safe_file_id,
            sandbox_context,
            reader,
            lambda: locator,
        )

    def _prepare_with_locator_provider(
        self,
        safe_file_id: str,
        sandbox_context: dict[str, Any],
        reader: "SandboxedOssClient",
        locator_provider: Callable[[], dict[str, Any]],
    ) -> PreparedSandboxFile:
        if not isinstance(sandbox_context, dict):
            raise RuntimeError("Sandbox context is invalid.")
        task_id = _safe_cache_id(sandbox_context.get("task_id"), "task_id")
        context_id = _sandbox_cache_context_id(sandbox_context)
        cache_key = (task_id, context_id, safe_file_id)
        with self._lock:
            if task_id in self._cleaning_tasks:
                raise RuntimeError("Task attachment cache cleanup is in progress.")
            prepared = self._prepared.get(cache_key)
            if prepared is not None and prepared.local_path.is_file():
                return prepared
            download_lock = self._download_locks.setdefault(
                cache_key,
                threading.Lock(),
            )

        with download_lock:
            with self._lock:
                if task_id in self._cleaning_tasks:
                    raise RuntimeError("Task attachment cache cleanup is in progress.")
                prepared = self._prepared.get(cache_key)
                if prepared is not None and prepared.local_path.is_file():
                    return prepared
                if prepared is not None:
                    self._prepared.pop(cache_key, None)
                    self._quota_committed.pop(cache_key, None)

            quota_reserved = False
            try:
                locator = locator_provider()
                if not isinstance(locator, dict):
                    raise RuntimeError(
                        "Runtime returned invalid attachment locator.",
                    )
                locator_file_id = _safe_cache_id(
                    locator.get("file_id"),
                    "file_id",
                )
                if locator_file_id != safe_file_id:
                    raise RuntimeError(
                        "Runtime returned mismatched attachment locator.",
                    )
                _assert_locator_within_size_limit(locator)
                original_name = _safe_original_name(locator.get("original_name"))
                self._reserve_task_quota(cache_key, locator)
                quota_reserved = True
                task_root = self._task_root(task_id)
                target = (
                    task_root
                    / "files"
                    / safe_file_id
                    / "contexts"
                    / context_id
                    / original_name
                )
                self._ensure_task_marker(task_id, context_id)
                self._ensure_private_file_dir(task_id, safe_file_id, context_id)
                resolved_target = target.resolve(strict=False)
                self._assert_cache_path(resolved_target)

                size_bytes, content_type = _write_stream_atomic(
                    resolved_target,
                    lambda write_chunk: reader.stream_authorized_locator(
                        locator,
                        write_chunk,
                    ),
                    on_size=lambda next_size: self._grow_task_quota_reservation(
                        cache_key,
                        next_size,
                    ),
                )
                self._apply_file_permissions(resolved_target)
                self._commit_task_quota(cache_key, size_bytes)
                quota_reserved = False
                prepared = PreparedSandboxFile(
                    file_id=safe_file_id,
                    local_path=resolved_target,
                    content_type=content_type,
                    size_bytes=size_bytes,
                    original_name=original_name,
                    expires_at=str(locator.get("expires_at") or ""),
                )
                with self._lock:
                    self._prepared[cache_key] = prepared
                return prepared
            except Exception:
                with self._lock:
                    if quota_reserved:
                        self._quota_reservations.pop(cache_key, None)
                    if cache_key not in self._prepared:
                        self._download_locks.pop(cache_key, None)
                raise

    def _reserve_task_quota(
        self,
        cache_key: tuple[str, str, str],
        locator: dict[str, Any],
    ) -> None:
        task_id = cache_key[0]
        expected_size = _locator_size_bytes(locator) or 0
        with self._lock:
            if cache_key in self._quota_committed:
                return
            task_committed = {
                key: size
                for key, size in self._quota_committed.items()
                if key[0] == task_id
            }
            task_reserved = {
                key: size
                for key, size in self._quota_reservations.items()
                if key[0] == task_id
            }
            if cache_key not in task_reserved and (
                len(task_committed) + len(task_reserved) >= self.max_file_count
            ):
                raise RuntimeError("Runtime task attachment quota exceeded.")
            current_reserved = task_reserved.get(cache_key, 0)
            next_total = (
                sum(task_committed.values())
                + sum(task_reserved.values())
                - current_reserved
                + expected_size
            )
            if next_total > self.max_total_bytes:
                raise RuntimeError("Runtime task attachment quota exceeded.")
            self._quota_reservations[cache_key] = expected_size

    def _grow_task_quota_reservation(
        self,
        cache_key: tuple[str, str, str],
        next_size: int,
    ) -> None:
        with self._lock:
            if cache_key not in self._quota_reservations:
                raise RuntimeError("Runtime task attachment quota unavailable.")
            current_size = self._quota_reservations[cache_key]
            if next_size <= current_size:
                return
            task_id = cache_key[0]
            used_bytes = sum(
                size
                for key, size in self._quota_committed.items()
                if key[0] == task_id
            ) + sum(
                size
                for key, size in self._quota_reservations.items()
                if key[0] == task_id
            )
            if used_bytes + next_size - current_size > self.max_total_bytes:
                raise RuntimeError("Runtime task attachment quota exceeded.")
            self._quota_reservations[cache_key] = next_size

    def _commit_task_quota(
        self,
        cache_key: tuple[str, str, str],
        size_bytes: int,
    ) -> None:
        with self._lock:
            if cache_key not in self._quota_reservations:
                raise RuntimeError("Runtime task attachment quota unavailable.")
            self._quota_reservations.pop(cache_key, None)
            self._quota_committed[cache_key] = int(size_bytes)

    def cleanup_task(self, task_id: str) -> None:
        """Delete a task-local cache directory and forget prepared entries."""
        safe_task_id = _safe_cache_id(task_id, "task_id")
        cleanup_lock = self._register_cleanup_user(safe_task_id)
        cleanup_lock.acquire()
        try:
            self._cleanup_task_serialized(safe_task_id)
        finally:
            cleanup_lock.release()
            self._unregister_cleanup_user(safe_task_id, cleanup_lock)

    def _cleanup_task_if_idle(self, task_id: str) -> bool:
        """Atomically claim and clean one idle task without waiting for IO."""
        safe_task_id = _safe_cache_id(task_id, "task_id")
        with self._batch_condition:
            if (
                safe_task_id in self._cleaning_tasks
                or self._io_reservations.get(safe_task_id, 0) > 0
                or any(
                    batch_key[0] == safe_task_id
                    for batch_key in self._batch_lock_users
                )
            ):
                return False
            cleanup_lock = self._cleanup_locks.setdefault(
                safe_task_id,
                threading.Lock(),
            )
            self._cleanup_users[safe_task_id] = (
                self._cleanup_users.get(safe_task_id, 0) + 1
            )
            self._cleaning_tasks.add(safe_task_id)
            self._batch_condition.notify_all()

        cleanup_lock.acquire()
        try:
            self._cleanup_task_serialized(safe_task_id)
        finally:
            cleanup_lock.release()
            self._unregister_cleanup_user(safe_task_id, cleanup_lock)
        return True

    def _register_cleanup_user(self, task_id: str) -> threading.Lock:
        with self._batch_condition:
            cleanup_lock = self._cleanup_locks.setdefault(
                task_id,
                threading.Lock(),
            )
            self._cleanup_users[task_id] = self._cleanup_users.get(task_id, 0) + 1
            self._cleaning_tasks.add(task_id)
            self._batch_condition.notify_all()
            return cleanup_lock

    def _unregister_cleanup_user(
        self,
        task_id: str,
        cleanup_lock: threading.Lock,
    ) -> None:
        with self._batch_condition:
            remaining = self._cleanup_users.get(task_id, 0) - 1
            if remaining > 0:
                self._cleanup_users[task_id] = remaining
            else:
                self._cleanup_users.pop(task_id, None)
                self._cleaning_tasks.discard(task_id)
                if self._cleanup_locks.get(task_id) is cleanup_lock:
                    self._cleanup_locks.pop(task_id, None)
            self._batch_condition.notify_all()

    def _cleanup_task_serialized(self, safe_task_id: str) -> None:
        locks_to_wait: list[threading.Lock]
        with self._batch_condition:
            self._batch_condition.wait_for(
                lambda: (
                    self._io_reservations.get(safe_task_id, 0) == 0
                    and not any(
                        batch_key[0] == safe_task_id
                        for batch_key in self._batch_lock_users
                    )
                ),
            )
            for batch_key in list(self._batch_locks):
                if batch_key[0] == safe_task_id:
                    self._batch_locks.pop(batch_key, None)
            locks_to_wait = [
                lock
                for cache_key, lock in self._download_locks.items()
                if cache_key[0] == safe_task_id
            ]
        for download_lock in locks_to_wait:
            download_lock.acquire()
        try:
            task_root = self._task_root(safe_task_id)
            self._assert_cache_path(task_root)
            shutil.rmtree(task_root, ignore_errors=True)
            with self._lock:
                self._markers.pop(safe_task_id, None)
                for cache_key in list(self._prepared):
                    if cache_key[0] == safe_task_id:
                        self._prepared.pop(cache_key, None)
                for cache_key in list(self._download_locks):
                    if cache_key[0] == safe_task_id:
                        self._download_locks.pop(cache_key, None)
                for cache_key in list(self._quota_committed):
                    if cache_key[0] == safe_task_id:
                        self._quota_committed.pop(cache_key, None)
                for cache_key in list(self._quota_reservations):
                    if cache_key[0] == safe_task_id:
                        self._quota_reservations.pop(cache_key, None)
        finally:
            for download_lock in reversed(locks_to_wait):
                download_lock.release()

    def sweep_expired(
        self,
        now_epoch: float | None = None,
        *,
        exclude_task_ids: set[str] | None = None,
    ) -> None:
        """Delete expired task-local cache directories."""
        now = time.time() if now_epoch is None else float(now_epoch)
        excluded = exclude_task_ids or set()
        with self._lock:
            markers = list(self._markers.items())
        markers.extend(
            (marker.task_id, marker)
            for marker in self._read_task_markers_from_disk()
        )
        expired_task_ids = {
            task_id
            for task_id, marker in markers
            if marker.expires_at_epoch <= now and task_id not in excluded
        }
        for task_id in expired_task_ids:
            self._cleanup_task_if_idle(task_id)

    def _task_root(self, task_id: str) -> Path:
        return self.root / task_id

    def _task_marker_path(self, task_id: str) -> Path:
        return self._task_root(task_id) / _TASK_MARKER_FILENAME

    def _write_task_marker(self, marker: TaskMarker) -> None:
        marker_path = self._task_marker_path(marker.task_id)
        self._ensure_private_dir(marker_path.parent)
        payload = {
            "task_id": marker.task_id,
            "sandbox_context_id": marker.sandbox_context_id,
            "created_at_epoch": marker.created_at_epoch,
            "expires_at_epoch": marker.expires_at_epoch,
        }
        _write_atomic(
            marker_path,
            json.dumps(payload, sort_keys=True).encode("utf-8"),
        )
        self._apply_file_permissions(marker_path)

    def _read_task_markers_from_disk(self) -> list[TaskMarker]:
        if not self.root.is_dir():
            return []
        markers: list[TaskMarker] = []
        for marker_path in self.root.glob(f"*/{_TASK_MARKER_FILENAME}"):
            marker = self._read_task_marker(marker_path)
            if marker is not None:
                markers.append(marker)
        return markers

    def _read_task_marker(self, marker_path: Path) -> TaskMarker | None:
        try:
            task_id_from_path = _safe_cache_id(marker_path.parent.name, "task_id")
            expected_marker_path = self._task_marker_path(task_id_from_path).resolve(strict=False)
            actual_marker_path = marker_path.resolve(strict=False)
            if actual_marker_path != expected_marker_path:
                return None
            self._assert_cache_path(actual_marker_path)
            if marker_path.stat().st_size > 4096:
                return None
            payload = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, RuntimeError):
            return None
        if not isinstance(payload, dict):
            return None
        try:
            task_id = _safe_cache_id(payload.get("task_id"), "task_id")
            context_id = _safe_cache_id(
                payload.get("sandbox_context_id"),
                "sandbox_context_id",
            )
            created_at_epoch = float(payload.get("created_at_epoch"))
            expires_at_epoch = float(payload.get("expires_at_epoch"))
        except (RuntimeError, TypeError, ValueError):
            return None
        if task_id != task_id_from_path:
            return None
        return TaskMarker(
            task_id=task_id,
            sandbox_context_id=context_id,
            created_at_epoch=created_at_epoch,
            expires_at_epoch=expires_at_epoch,
        )

    def _ensure_task_marker(self, task_id: str, context_id: str) -> TaskMarker:
        now_epoch = time.time()
        marker = TaskMarker(
            task_id=task_id,
            sandbox_context_id=context_id,
            created_at_epoch=now_epoch,
            expires_at_epoch=now_epoch + self.ttl_seconds,
        )
        with self._lock:
            if task_id in self._cleaning_tasks:
                raise RuntimeError("Task attachment cache cleanup is in progress.")
            self._ensure_private_task_root(task_id)
            self._write_task_marker(marker)
            self._markers[task_id] = marker
        return marker

    def _ensure_private_task_root(self, task_id: str) -> None:
        task_root = self._task_root(task_id)
        for directory in (
            self.root,
            task_root,
        ):
            self._ensure_private_dir(directory)

    def _ensure_private_file_dir(self, task_id: str, file_id: str, context_id: str) -> None:
        task_root = self._task_root(task_id)
        self._ensure_private_task_root(task_id)
        for directory in (
            task_root / "files",
            task_root / "files" / file_id,
            task_root / "files" / file_id / "contexts",
            task_root / "files" / file_id / "contexts" / context_id,
        ):
            self._ensure_private_dir(directory)

    def _ensure_private_dir(self, path: Path) -> None:
        self._assert_cache_path(path)
        _mkdir_private(path)
        os.chmod(path, 0o700)

    def _apply_file_permissions(self, path: Path) -> None:
        os.chmod(path, 0o600)

    def _assert_cache_path(self, path: Path) -> None:
        root = self.root.resolve(strict=False)
        resolved = Path(path).resolve(strict=False)
        if root != resolved and root not in resolved.parents:
            raise RuntimeError("Sandbox attachment cache path is invalid.")


async def run_task_io_in_thread(
    cache: TaskAttachmentCache,
    task_id: str,
    function: Callable[..., Any],
    *args: Any,
    file_id: str = "",
    **kwargs: Any,
) -> Any:
    """Run task IO in a thread while cleanup waits for submission and exit."""
    reservation = cache.reserve_task_io(task_id, file_id=file_id)
    try:
        worker = asyncio.create_task(
            _run_in_thread(function, *args, **kwargs),
        )
    except BaseException:
        reservation.release()
        raise

    cancelled = False
    worker_error: BaseException | None = None
    result: Any = None
    try:
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                cancelled = True
            except BaseException:
                break
        try:
            result = worker.result()
        except BaseException as exc:  # consume worker failure before release
            worker_error = exc
    finally:
        reservation.release()

    if cancelled:
        raise asyncio.CancelledError() from None
    if worker_error is not None:
        raise worker_error
    return result


class SandboxedOssClient:
    """Read a Runtime-authorized object without exposing locators to the model."""

    def __init__(
        self,
        *,
        runtime_base_url: str | None = None,
        service_token: str | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self.runtime_base_url = (
            runtime_base_url or _runtime_base_url_from_context_or_env()
        )
        self.service_token = (
            service_token
            or os.environ.get("QWENPAW_SERVICE_TOKEN")
            or os.environ.get("RUNTIME_QWENPAW_SERVICE_TOKEN")
            or ""
        ).strip()
        self.timeout_seconds = timeout_seconds or _runtime_timeout_seconds()

    def read_file(
        self,
        file_id: str,
        sandbox_context: dict[str, Any],
        *,
        max_bytes: int | None = None,
    ) -> SandboxedObjectContent:
        """Authorize then read one file in the current user-assistant sandbox."""
        if not isinstance(sandbox_context, dict):
            raise RuntimeError("Sandbox context is invalid.")
        safe_file_id = _safe_cache_id(file_id, "file_id")
        task_id = _safe_cache_id(sandbox_context.get("task_id"), "task_id")
        with _DEFAULT_TASK_ATTACHMENT_CACHE.reserve_task_io(
            task_id,
            file_id=safe_file_id,
        ):
            prepared = _DEFAULT_TASK_ATTACHMENT_CACHE.prepare_file(
                safe_file_id,
                sandbox_context,
                client=self,
            )
            size_bytes = prepared.local_path.stat().st_size
            if max_bytes is None:
                read_limit = None
            else:
                try:
                    read_limit = int(max_bytes)
                except (TypeError, ValueError) as exc:
                    raise RuntimeError(
                        "Sandbox attachment read limit is invalid.",
                    ) from exc
                if read_limit < 0:
                    raise RuntimeError(
                        "Sandbox attachment read limit is invalid.",
                    )
            with prepared.local_path.open("rb") as handle:
                content = (
                    handle.read()
                    if read_limit is None
                    else handle.read(read_limit)
                )
            return SandboxedObjectContent(
                content=content,
                content_type=prepared.content_type,
                size_bytes=size_bytes,
            )

    def authorize_file(
        self,
        file_id: str,
        sandbox_context: dict[str, Any],
    ) -> dict[str, Any]:
        parsed = self._post_json(
            "/runtime/internal/sandbox/attachments/authorize",
            {
                "file_id": str(file_id or "").strip(),
                "sandbox_context": sandbox_context,
            },
        )
        return _extract_locator(parsed)

    def authorize_files(
        self,
        file_ids: list[str],
        sandbox_context: dict[str, Any],
        *,
        selection_records: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        """Authorize multiple sandbox files in one Runtime request."""
        ordered = list(
            dict.fromkeys(
                _safe_cache_id(file_id, "file_id")
                for file_id in file_ids
            ),
        )
        payload: dict[str, Any] = {
            "sandbox_context": sandbox_context,
            "file_ids": ordered,
        }
        if selection_records:
            payload["selection_records"] = list(selection_records)
        parsed = self._post_json(
            "/runtime/internal/sandbox/attachments/batch-authorize",
            payload,
        )
        data = parsed.get("data", parsed)
        if not isinstance(data, dict):
            raise RuntimeError("Runtime returned invalid batch authorization.")
        authorized = data.get("authorized", [])
        denied = data.get("denied", [])
        if not isinstance(authorized, list) or not isinstance(denied, list):
            raise RuntimeError("Runtime returned invalid batch authorization.")
        if not all(isinstance(item, dict) for item in authorized + denied):
            raise RuntimeError("Runtime returned invalid batch authorization.")
        return {"authorized": authorized, "denied": denied}

    def search_files(
        self,
        query: str,
        content_types: list[str],
        sources: list[str],
        limit: int,
        sandbox_context: dict[str, Any],
        *,
        extensions: list[str] | None = None,
    ) -> dict[str, Any]:
        """Search supplemental files inside the signed Runtime sandbox."""
        parsed = self._post_json(
            "/runtime/internal/sandbox/files/search",
            {
                "sandbox_context": sandbox_context,
                "query": query,
                "content_types": content_types,
                "extensions": list(extensions or []),
                "sources": sources,
                "limit": limit,
            },
        )
        data = parsed.get("data", parsed)
        if not isinstance(data, dict) or not isinstance(
            data.get("files", []),
            list,
        ):
            raise RuntimeError("Runtime returned invalid sandbox search results.")
        return data

    def expand_conversation_context(
        self,
        before_turn_id: str,
        limit: int,
        sandbox_context: dict[str, Any],
    ) -> dict[str, Any]:
        """Fetch earlier messages from the active Runtime conversation."""
        parsed = self._post_json(
            "/runtime/internal/conversation/context/expand",
            {
                "sandbox_context": sandbox_context,
                "before_turn_id": before_turn_id,
                "limit": limit,
            },
        )
        data = parsed.get("data", parsed)
        if not isinstance(data, dict) or not isinstance(
            data.get("messages", []),
            list,
        ):
            raise RuntimeError("Runtime returned invalid conversation context.")
        return data

    def _post_json(
        self,
        path: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if not self.runtime_base_url:
            raise RuntimeError("Runtime base URL is unavailable.")
        if not self.service_token:
            raise RuntimeError("Runtime service token is unavailable.")
        request = urllib.request.Request(
            urljoin(
                self.runtime_base_url.rstrip("/") + "/",
                str(path or "").lstrip("/"),
            ),
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {self.service_token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=self.timeout_seconds,
            ) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raise RuntimeError("Runtime rejected sandbox attachment read.") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError("Runtime is unreachable.") from exc
        try:
            parsed = json.loads(body) if body else {}
        except json.JSONDecodeError as exc:
            raise RuntimeError("Runtime returned invalid JSON.") from exc
        if not isinstance(parsed, dict):
            raise RuntimeError("Runtime returned invalid JSON.")
        return parsed

    def stream_authorized_locator(
        self,
        locator: dict[str, Any],
        write_chunk: Callable[[bytes], None],
    ) -> str:
        """Stream one authorized object to a bounded cache writer."""
        provider = str(locator.get("storage_provider", "")).strip().lower()
        object_key = str(locator.get("object_key", "")).strip()
        _validate_object_key(object_key)
        if provider == "local":
            return _stream_local_object(locator, object_key, write_chunk)
        if provider == "oss":
            return _stream_oss_object(locator, object_key, write_chunk)
        raise RuntimeError("Unsupported sandbox attachment storage provider.")


def content_part_for_prepared_file(prepared: PreparedSandboxFile) -> dict[str, Any]:
    """Convert a prepared task-local file into a QwenPaw content part."""
    url = prepared.local_path.resolve().as_uri()
    content_type = prepared.content_type.lower()
    runtime_fields = {
        "_runtime_sandbox_attachment": True,
        "_runtime_attachment_file_id": prepared.file_id,
    }
    if content_type.startswith("image/"):
        return {
            "type": "image",
            "source": {"type": "url", "url": url},
            **runtime_fields,
        }
    if content_type.startswith("video/"):
        return {
            "type": "video",
            "source": {"type": "url", "url": url},
            **runtime_fields,
        }
    if content_type.startswith("audio/"):
        return {
            "type": "audio",
            "source": {"type": "url", "url": url},
            **runtime_fields,
        }
    return {
        "type": "file",
        "filename": prepared.original_name,
        "source": {"type": "url", "url": url},
        **runtime_fields,
    }


def _runtime_base_url_from_context_or_env() -> str:
    gateway = get_current_runtime_tool_gateway()
    return resolve_runtime_base_url(gateway)


def _runtime_timeout_seconds() -> float:
    gateway = get_current_runtime_tool_gateway()
    if isinstance(gateway, dict):
        try:
            return max(float(gateway.get("timeout_seconds", 30)), 0.1)
        except (TypeError, ValueError):
            pass
    return 30.0


def _task_file_ttl_seconds(value: int | float | None = None) -> float:
    if value is None:
        value = os.environ.get("QWENPAW_TASK_FILE_TTL_SECONDS", "1800")
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 1800.0
    return parsed if parsed > 0 else 1800.0


def _task_file_sweep_interval_seconds(
    value: int | float | None = None,
) -> float:
    if value is None:
        value = os.environ.get(
            "QWENPAW_TASK_FILE_SWEEP_INTERVAL_SECONDS",
            "60",
        )
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 60.0
    return parsed if parsed > 0 else 60.0


def _task_file_max_bytes() -> int:
    value = os.environ.get("QWENPAW_TASK_FILE_MAX_BYTES", "209715200")
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 209715200
    return parsed if parsed > 0 else 209715200


def _task_file_max_count() -> int:
    value = os.environ.get("QWENPAW_TASK_FILE_MAX_COUNT", "20")
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 20
    return parsed if parsed > 0 else 20


def _task_file_max_total_bytes() -> int:
    value = os.environ.get(
        "QWENPAW_TASK_FILE_MAX_TOTAL_BYTES",
        "209715200",
    )
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 209715200
    return parsed if parsed > 0 else 209715200


def _locator_size_bytes(locator: dict[str, Any]) -> int | None:
    value = locator.get("size_bytes")
    if value in (None, ""):
        return None
    try:
        size_bytes = int(value)
    except (TypeError, ValueError):
        raise RuntimeError("Sandbox attachment size is invalid.") from None
    if size_bytes < 0:
        raise RuntimeError("Sandbox attachment size is invalid.")
    return size_bytes


def _assert_locator_within_size_limit(locator: dict[str, Any]) -> None:
    size_bytes = _locator_size_bytes(locator)
    if size_bytes is None:
        return
    _assert_content_within_size_limit(size_bytes)


def _assert_content_within_size_limit(size_bytes: int) -> None:
    if int(size_bytes) > _task_file_max_bytes():
        raise RuntimeError("Sandbox attachment is too large.")


def _safe_cache_id(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    if (
        not text
        or text in {".", ".."}
        or text != str(value or "")
        or len(text) > 128
        or "/" in text
        or "\\" in text
        or "\x00" in text
        or any(not (ch.isalnum() or ch in {"_", "-"}) for ch in text)
    ):
        raise RuntimeError(f"Sandbox attachment {field_name} is invalid.")
    return text


def _sandbox_cache_context_id(sandbox_context: dict[str, Any]) -> str:
    """Resolve legacy and runtime-worker/v1 task cache identities."""
    return _safe_cache_id(
        sandbox_context.get("context_id")
        or sandbox_context.get("sandbox_context_id")
        or sandbox_context.get("sandbox_instance_id")
        or sandbox_context.get("context_manifest_id"),
        "sandbox_context_id",
    )


def _safe_original_name(value: Any) -> str:
    raw = str(value or "").replace("\\", "/").strip()
    name = Path(raw).name
    if (
        not name
        or name in {".", ".."}
        or name == _TASK_MARKER_FILENAME
        or "/" in name
        or "\\" in name
        or "\x00" in name
    ):
        return "attachment"
    encoded_name = name.encode("utf-8")
    if len(encoded_name) <= _MAX_ORIGINAL_NAME_BYTES:
        return name
    suffix = Path(name).suffix
    if not (
        1 < len(suffix) <= _MAX_SAFE_EXTENSION_CHARS + 1
        and suffix[1:].isascii()
        and suffix[1:].isalnum()
    ):
        suffix = ""
    suffix_bytes = suffix.encode("utf-8")
    stem = name[: -len(suffix)] if suffix else name
    stem_budget = _MAX_ORIGINAL_NAME_BYTES - len(suffix_bytes)
    truncated_stem = stem.encode("utf-8")[:stem_budget].decode(
        "utf-8",
        errors="ignore",
    )
    return f"{truncated_stem or 'attachment'}{suffix}"


def _write_atomic(target: Path, content: bytes) -> None:
    temp_path = target.with_name(f"{target.name}.part.{uuid.uuid4().hex}")
    fd: int | None = None
    try:
        fd = os.open(
            temp_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(fd, "wb") as handle:
            fd = None
            handle.write(content)
        os.replace(temp_path, target)
        os.chmod(target, 0o600)
    finally:
        if fd is not None:
            os.close(fd)
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def _write_stream_atomic(
    target: Path,
    stream_to: Callable[[Callable[[bytes], None]], str],
    *,
    on_size: Callable[[int], None] | None = None,
) -> tuple[int, str]:
    temp_path = target.with_name(f"{target.name}.part.{uuid.uuid4().hex}")
    fd: int | None = None
    size_bytes = 0
    committed = False
    try:
        fd = os.open(
            temp_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(fd, "wb") as handle:
            fd = None

            def write_chunk(chunk: bytes) -> None:
                nonlocal size_bytes
                if not isinstance(chunk, bytes):
                    raise RuntimeError(
                        "Sandbox attachment stream returned invalid content.",
                    )
                next_size = size_bytes + len(chunk)
                _assert_content_within_size_limit(next_size)
                if on_size is not None:
                    on_size(next_size)
                handle.write(chunk)
                size_bytes = next_size

            content_type = str(stream_to(write_chunk) or "")
            handle.flush()
        os.replace(temp_path, target)
        os.chmod(target, 0o600)
        committed = True
        return size_bytes, content_type
    finally:
        if fd is not None:
            os.close(fd)
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
        if not committed:
            try:
                target.unlink()
            except FileNotFoundError:
                pass


def _extract_locator(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise RuntimeError("Runtime returned invalid attachment locator.")
    data = payload.get("data")
    if isinstance(data, dict):
        return data
    if "storage_provider" in payload and "object_key" in payload:
        return payload
    raise RuntimeError("Runtime returned invalid attachment locator.")


def _stream_local_object(
    locator: dict[str, Any],
    object_key: str,
    write_chunk: Callable[[bytes], None],
) -> str:
    root_value = (
        os.environ.get("QWENPAW_LOCAL_OBJECT_ROOT")
        or os.environ.get("RUNTIME_LOCAL_OBJECT_ROOT")
        or os.environ.get("LOCAL_OBJECT_ROOT")
        or ""
    ).strip()
    if not root_value:
        raise RuntimeError("Local object root is unavailable.")
    root = Path(root_value).expanduser().resolve()
    target = (root / object_key).resolve()
    if root != target and root not in target.parents:
        raise RuntimeError("Sandbox attachment path is invalid.")
    if not target.is_file():
        raise RuntimeError("Sandbox attachment is not a regular file.")
    _assert_content_within_size_limit(target.stat().st_size)
    with target.open("rb") as handle:
        while True:
            chunk = handle.read(_DOWNLOAD_CHUNK_BYTES)
            if not chunk:
                break
            write_chunk(chunk)
    return str(locator.get("content_type") or "")


def _stream_oss_object(
    locator: dict[str, Any],
    object_key: str,
    write_chunk: Callable[[bytes], None],
) -> str:
    endpoint = os.environ.get("OSS_ENDPOINT", "").strip()
    if endpoint and "://" not in endpoint:
        endpoint = f"https://{endpoint}"
    bucket_name = str(
        locator.get("bucket") or os.environ.get("OSS_BUCKET", ""),
    ).strip()
    access_key_id = os.environ.get("OSS_ACCESS_KEY_ID", "").strip()
    access_key_secret = os.environ.get("OSS_ACCESS_KEY_SECRET", "").strip()
    if not all((endpoint, bucket_name, access_key_id, access_key_secret)):
        raise RuntimeError("OSS reader is not configured.")
    try:
        import oss2
    except ImportError as exc:
        raise RuntimeError("OSS reader dependency is unavailable.") from exc
    auth = oss2.Auth(access_key_id, access_key_secret)
    bucket = oss2.Bucket(auth, endpoint, bucket_name)
    _assert_locator_within_size_limit(locator)
    object_stream = bucket.get_object(object_key)
    try:
        while True:
            chunk = object_stream.read(_DOWNLOAD_CHUNK_BYTES)
            if not chunk:
                break
            write_chunk(chunk)
    finally:
        close = getattr(object_stream, "close", None)
        if callable(close):
            close()
    return str(locator.get("content_type") or "")


def _validate_object_key(object_key: str) -> None:
    key = str(object_key or "")
    parts = key.split("/")
    if (
        not key
        or key != key.strip()
        or key.startswith("/")
        or key.startswith("./")
        or key.endswith("/")
        or "\\" in key
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise RuntimeError("Sandbox attachment object key is invalid.")


def _mkdir_private(path: Path) -> None:
    target = Path(path)
    missing: list[Path] = []
    current = target
    while not current.exists():
        missing.append(current)
        parent = current.parent
        if parent == current:
            break
        current = parent
    for directory in reversed(missing):
        try:
            os.mkdir(directory, 0o700)
        except FileExistsError:
            if not directory.is_dir():
                raise
    if target.is_dir():
        os.chmod(target, 0o700)


_DEFAULT_TASK_ATTACHMENT_CACHE = TaskAttachmentCache()
_DEFAULT_TASK_ATTACHMENT_CACHE.start_sweeper()
atexit.register(_DEFAULT_TASK_ATTACHMENT_CACHE.stop_sweeper)
