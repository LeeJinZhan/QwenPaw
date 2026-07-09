# -*- coding: utf-8 -*-
"""Runtime-authorized object reader for sandbox attachments."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
import uuid
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse

from ...config.context import get_current_runtime_tool_gateway


_TASK_MARKER_FILENAME = ".task-marker.json"


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
        self._prepared: dict[tuple[str, str, str], PreparedSandboxFile] = {}
        self._markers: dict[str, TaskMarker] = {}
        self._lock = threading.RLock()
        self._download_locks: dict[tuple[str, str, str], threading.Lock] = {}
        self._cleaning_tasks: set[str] = set()

    def prepare_file(
        self,
        file_id: str,
        sandbox_context: dict[str, Any],
        *,
        client: "SandboxedOssClient | None" = None,
    ) -> PreparedSandboxFile:
        """Authorize, download once, and return the task-local file path."""
        if not isinstance(sandbox_context, dict):
            raise RuntimeError("Sandbox context is invalid.")
        safe_file_id = _safe_cache_id(file_id, "file_id")
        task_id = _safe_cache_id(sandbox_context.get("task_id"), "task_id")
        context_id = _safe_cache_id(
            sandbox_context.get("context_id")
            or sandbox_context.get("sandbox_context_id"),
            "sandbox_context_id",
        )
        self.sweep_expired()
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

            try:
                reader = client or SandboxedOssClient()
                locator = reader.authorize_file(safe_file_id, sandbox_context)
                _assert_locator_within_size_limit(locator)
                original_name = _safe_original_name(locator.get("original_name"))
                task_root = self._task_root(task_id)
                target = (
                    task_root
                    / "files"
                    / safe_file_id
                    / "contexts"
                    / context_id
                    / original_name
                )
                self._ensure_private_file_dir(task_id, safe_file_id, context_id)
                resolved_target = target.resolve(strict=False)
                self._assert_cache_path(resolved_target)

                content = reader.read_authorized_locator(locator)
                _assert_content_within_size_limit(len(content.content))
                _write_atomic(resolved_target, content.content)
                now_epoch = time.time()
                marker = TaskMarker(
                    task_id=task_id,
                    sandbox_context_id=context_id,
                    created_at_epoch=now_epoch,
                    expires_at_epoch=now_epoch + self.ttl_seconds,
                )
                prepared = PreparedSandboxFile(
                    file_id=safe_file_id,
                    local_path=resolved_target,
                    content_type=content.content_type,
                    size_bytes=len(content.content),
                    original_name=original_name,
                    expires_at=str(locator.get("expires_at") or ""),
                )
                self._write_task_marker(marker)
                with self._lock:
                    self._markers[task_id] = marker
                    self._prepared[cache_key] = prepared
                return prepared
            except Exception:
                with self._lock:
                    if cache_key not in self._prepared:
                        self._download_locks.pop(cache_key, None)
                raise

    def cleanup_task(self, task_id: str) -> None:
        """Delete a task-local cache directory and forget prepared entries."""
        safe_task_id = _safe_cache_id(task_id, "task_id")
        locks_to_wait: list[threading.Lock]
        with self._lock:
            self._cleaning_tasks.add(safe_task_id)
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
        finally:
            for download_lock in reversed(locks_to_wait):
                download_lock.release()
            with self._lock:
                self._cleaning_tasks.discard(safe_task_id)

    def sweep_expired(self, now_epoch: float | None = None) -> None:
        """Delete expired task-local cache directories."""
        now = time.time() if now_epoch is None else float(now_epoch)
        with self._lock:
            markers = list(self._markers.items())
        markers.extend(
            (marker.task_id, marker)
            for marker in self._read_task_markers_from_disk()
        )
        expired_task_ids = {
            task_id
            for task_id, marker in markers
            if marker.expires_at_epoch <= now
        }
        for task_id in expired_task_ids:
            self.cleanup_task(task_id)

    def _task_root(self, task_id: str) -> Path:
        shard = self._task_shard(task_id)
        return self.root / "shard" / shard / "task" / task_id

    @staticmethod
    def _task_shard(task_id: str) -> str:
        return hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:2]

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

    def _read_task_markers_from_disk(self) -> list[TaskMarker]:
        if not self.root.is_dir():
            return []
        markers: list[TaskMarker] = []
        for marker_path in self.root.glob(f"shard/*/task/*/{_TASK_MARKER_FILENAME}"):
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

    def _ensure_private_file_dir(self, task_id: str, file_id: str, context_id: str) -> None:
        shard = self._task_shard(task_id)
        task_root = self._task_root(task_id)
        for directory in (
            self.root,
            self.root / "shard",
            self.root / "shard" / shard,
            self.root / "shard" / shard / "task",
            task_root,
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

    def _assert_cache_path(self, path: Path) -> None:
        root = self.root.resolve(strict=False)
        resolved = Path(path).resolve(strict=False)
        if root != resolved and root not in resolved.parents:
            raise RuntimeError("Sandbox attachment cache path is invalid.")


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
    ) -> SandboxedObjectContent:
        """Authorize then read one file in the current user-assistant sandbox."""
        prepared = _DEFAULT_TASK_ATTACHMENT_CACHE.prepare_file(
            file_id,
            sandbox_context,
            client=self,
        )
        content = prepared.local_path.read_bytes()
        return SandboxedObjectContent(
            content=content,
            content_type=prepared.content_type,
            size_bytes=len(content),
        )

    def authorize_file(
        self,
        file_id: str,
        sandbox_context: dict[str, Any],
    ) -> dict[str, Any]:
        if not self.runtime_base_url:
            raise RuntimeError("Runtime base URL is unavailable.")
        if not self.service_token:
            raise RuntimeError("Runtime service token is unavailable.")
        payload = {
            "file_id": str(file_id or "").strip(),
            "sandbox_context": sandbox_context,
        }
        request = urllib.request.Request(
            urljoin(
                self.runtime_base_url.rstrip("/") + "/",
                "runtime/internal/sandbox/attachments/authorize",
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
            raise RuntimeError("Runtime returned invalid attachment locator.") from exc
        return _extract_locator(parsed)

    def read_authorized_locator(
        self,
        locator: dict[str, Any],
    ) -> SandboxedObjectContent:
        provider = str(locator.get("storage_provider", "")).strip().lower()
        object_key = str(locator.get("object_key", "")).strip()
        _validate_object_key(object_key)
        if provider == "local":
            return _read_local_object(locator, object_key)
        if provider == "oss":
            return _read_oss_object(locator, object_key)
        raise RuntimeError("Unsupported sandbox attachment storage provider.")


def _runtime_base_url_from_context_or_env() -> str:
    for key in (
        "QWENPAW_RUNTIME_BASE_URL",
        "BANK_RUNTIME_BASE_URL",
        "RUNTIME_BASE_URL",
    ):
        value = os.environ.get(key, "").strip()
        if value:
            return value
    gateway = get_current_runtime_tool_gateway()
    if not isinstance(gateway, dict):
        return ""
    base_url = str(
        gateway.get("base_url") or gateway.get("runtime_base_url") or "",
    ).strip()
    if base_url:
        return base_url
    endpoint = str(gateway.get("endpoint", "")).strip()
    parsed = urlparse(endpoint)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))
    return ""


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


def _task_file_max_bytes() -> int:
    value = os.environ.get("QWENPAW_TASK_FILE_MAX_BYTES", "209715200")
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 209715200
    return parsed if parsed > 0 else 209715200


def _assert_locator_within_size_limit(locator: dict[str, Any]) -> None:
    value = locator.get("size_bytes")
    if value in (None, ""):
        return
    try:
        size_bytes = int(value)
    except (TypeError, ValueError):
        raise RuntimeError("Sandbox attachment size is invalid.") from None
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
    return name


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


def _extract_locator(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise RuntimeError("Runtime returned invalid attachment locator.")
    data = payload.get("data")
    if isinstance(data, dict):
        return data
    if "storage_provider" in payload and "object_key" in payload:
        return payload
    raise RuntimeError("Runtime returned invalid attachment locator.")


def _read_local_object(
    locator: dict[str, Any],
    object_key: str,
) -> SandboxedObjectContent:
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
    max_bytes = _task_file_max_bytes()
    with target.open("rb") as handle:
        content = handle.read(max_bytes + 1)
    _assert_content_within_size_limit(len(content))
    return SandboxedObjectContent(
        content=content,
        content_type=str(locator.get("content_type") or ""),
        size_bytes=len(content),
    )


def _read_oss_object(
    locator: dict[str, Any],
    object_key: str,
) -> SandboxedObjectContent:
    endpoint = os.environ.get("OSS_ENDPOINT", "").strip()
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
    content = bucket.get_object(object_key).read(_task_file_max_bytes() + 1)
    _assert_content_within_size_limit(len(content))
    return SandboxedObjectContent(
        content=content,
        content_type=str(locator.get("content_type") or ""),
        size_bytes=len(content),
    )


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
