"""Bounded task-private cache for Runtime-authorized attachment bytes."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import shutil
from typing import Any
import uuid

from .scope import SandboxRequestScope

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


class SandboxCacheError(RuntimeError):
    """Attachment authorization or materialization failed safely."""


@dataclass(frozen=True)
class PreparedSandboxFile:
    file_id: str
    local_path: Path
    content_type: str
    size_bytes: int
    original_name: str
    expires_at: str


class TaskAttachmentCache:
    def __init__(
        self,
        root: str | Path | None = None,
        *,
        max_files: int = 20,
        max_total_bytes: int = 64 * 1024 * 1024,
        max_file_bytes: int = 32 * 1024 * 1024,
    ) -> None:
        self.root = (
            Path(
                root
                or os.environ.get("QWENPAW_TASK_FILE_ROOT")
                or "/tmp/qwenpaw-runtime-task-files"
            )
            .expanduser()
            .resolve()
        )
        self.max_files = max(1, min(int(max_files), 100))
        self.max_total_bytes = max(1, int(max_total_bytes))
        self.max_file_bytes = max(1, min(int(max_file_bytes), self.max_total_bytes))
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        self._locks: dict[str, asyncio.Lock] = {}
        self._prepared: dict[tuple[str, str], PreparedSandboxFile] = {}

    async def prepare_files(
        self,
        scope: SandboxRequestScope,
        file_ids: list[str],
        broker: Any,
        *,
        selection_records: list[dict[str, str]] | None = None,
    ) -> list[PreparedSandboxFile]:
        ordered = list(dict.fromkeys(str(value or "").strip() for value in file_ids))
        if not ordered or any(not value for value in ordered):
            raise SandboxCacheError("Attachment IDs are invalid")
        if len(ordered) != len(file_ids):
            raise SandboxCacheError("Attachment IDs contain duplicates")
        if len(ordered) > self.max_files:
            raise SandboxCacheError("Attachment file quota exceeded")
        lock = self._locks.setdefault(scope.task_id, asyncio.Lock())
        async with lock:
            task_prepared: dict[str, PreparedSandboxFile] = {}
            for key, item in list(self._prepared.items()):
                if key[0] != scope.task_id:
                    continue
                if item.local_path.is_file():
                    task_prepared[key[1]] = item
                else:
                    self._prepared.pop(key, None)
            new_ids = [file_id for file_id in ordered if file_id not in task_prepared]
            if len(task_prepared) + len(new_ids) > self.max_files:
                raise SandboxCacheError("Attachment file quota exceeded")
            cached = {
                file_id: self._prepared.get((scope.task_id, file_id))
                for file_id in ordered
            }
            if all(
                item is not None and item.local_path.is_file()
                for item in cached.values()
            ):
                return [cached[file_id] for file_id in ordered]  # type: ignore[misc]
            authorization = await broker.authorize_files(
                scope,
                ordered,
                selection_records=selection_records,
            )
            locators = self._validated_locators(authorization, ordered)
            total = sum(item.size_bytes for item in task_prepared.values()) + sum(
                _locator_size(locators[file_id]) for file_id in new_ids
            )
            if total > self.max_total_bytes:
                raise SandboxCacheError("Attachment byte quota exceeded")
            task_root = self._task_root(scope.task_id)
            task_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(task_root, 0o700)
            prepared: list[PreparedSandboxFile] = []
            try:
                for file_id in ordered:
                    item = self._prepared.get((scope.task_id, file_id))
                    if item is None or not item.local_path.is_file():
                        item = await _run_thread(
                            self._materialize,
                            task_root,
                            file_id,
                            locators[file_id],
                            broker,
                        )
                        self._prepared[(scope.task_id, file_id)] = item
                    prepared.append(item)
            except BaseException:
                await _run_thread(_safe_remove_tree, self.root, task_root)
                for key in [key for key in self._prepared if key[0] == scope.task_id]:
                    self._prepared.pop(key, None)
                raise
            return prepared

    async def cleanup(self, task_id: str) -> None:
        normalized = str(task_id or "").strip()
        lock = self._locks.setdefault(normalized, asyncio.Lock())
        async with lock:
            task_root = self._task_root(normalized)
            await _run_thread(_safe_remove_tree, self.root, task_root)
            for key in [key for key in self._prepared if key[0] == normalized]:
                self._prepared.pop(key, None)
        self._locks.pop(normalized, None)

    def _task_root(self, task_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", task_id):
            raise SandboxCacheError("Task cache ID is invalid")
        target = (self.root / task_id).resolve(strict=False)
        if target.parent != self.root:
            raise SandboxCacheError("Task cache path escaped root")
        return target

    def _validated_locators(
        self,
        value: Any,
        requested: list[str],
    ) -> dict[str, dict[str, Any]]:
        if not isinstance(value, dict):
            raise SandboxCacheError("Runtime authorization is invalid")
        authorized = value.get("authorized")
        denied = value.get("denied")
        if not isinstance(authorized, list) or not isinstance(denied, list):
            raise SandboxCacheError("Runtime authorization is invalid")
        if denied:
            raise SandboxCacheError("Runtime denied an attachment")
        if not all(isinstance(item, dict) for item in authorized):
            raise SandboxCacheError("Runtime authorization is invalid")
        locators = {str(item.get("file_id") or ""): item for item in authorized}
        if set(locators) != set(requested) or len(authorized) != len(requested):
            raise SandboxCacheError("Runtime authorization set mismatch")
        for locator in locators.values():
            size = _locator_size(locator)
            if size > self.max_file_bytes:
                raise SandboxCacheError("Attachment file quota exceeded")
        return locators

    def _materialize(
        self,
        task_root: Path,
        file_id: str,
        locator: dict[str, Any],
        broker: Any,
    ) -> PreparedSandboxFile:
        name = _safe_filename(
            locator.get("original_name") or locator.get("display_name")
        )
        suffix = Path(name).suffix[:16]
        target = (task_root / f"{file_id}{suffix}").resolve(strict=False)
        if target.parent != task_root or target.is_symlink():
            raise SandboxCacheError("Attachment cache path is invalid")
        temporary = target.with_name(f".{target.name}.part-{uuid.uuid4().hex}")
        digest = hashlib.sha256()
        written = 0

        def write_chunk(chunk: bytes) -> None:
            nonlocal written
            if not isinstance(chunk, bytes):
                raise SandboxCacheError("Attachment stream is invalid")
            written += len(chunk)
            if written > self.max_file_bytes or written > self.max_total_bytes:
                raise SandboxCacheError("Attachment byte quota exceeded")
            digest.update(chunk)
            handle.write(chunk)

        try:
            with temporary.open("xb") as handle:
                os.chmod(temporary, 0o600)
                broker.stream_locator(locator, write_chunk)
                handle.flush()
                os.fsync(handle.fileno())
            expected_size = _locator_size(locator)
            if written != expected_size:
                raise SandboxCacheError("Attachment size mismatch")
            expected_hash = str(locator.get("content_hash") or "").lower()
            actual_hash = digest.hexdigest()
            if expected_hash.removeprefix("sha256:") != actual_hash:
                raise SandboxCacheError("Attachment hash mismatch")
            os.replace(temporary, target)
            os.chmod(target, 0o600)
        except BaseException:
            temporary.unlink(missing_ok=True)
            target.unlink(missing_ok=True)
            raise
        return PreparedSandboxFile(
            file_id=file_id,
            local_path=target,
            content_type=str(locator.get("content_type") or "application/octet-stream")[
                :128
            ],
            size_bytes=written,
            original_name=name,
            expires_at=str(locator.get("expires_at") or "")[:64],
        )


def _locator_size(locator: dict[str, Any]) -> int:
    try:
        size = int(locator.get("size_bytes"))
    except (TypeError, ValueError) as exc:
        raise SandboxCacheError("Attachment size is invalid") from exc
    if size < 0:
        raise SandboxCacheError("Attachment size is invalid")
    return size


def _safe_filename(value: Any) -> str:
    name = str(value or "attachment").replace("\\", "/").split("/")[-1]
    name = _SAFE_NAME.sub("_", name).strip("._")[:180]
    return name or "attachment"


async def _run_thread(function: Any, *args: Any) -> Any:
    worker = asyncio.create_task(asyncio.to_thread(function, *args))
    cancelled = False
    while not worker.done():
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError:
            cancelled = True
    result = worker.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


def _safe_remove_tree(root: Path, target: Path) -> None:
    resolved_root = root.resolve()
    resolved_target = target.resolve(strict=False)
    if resolved_target.parent != resolved_root:
        raise SandboxCacheError("Task cleanup path escaped root")
    if resolved_target.exists():
        shutil.rmtree(resolved_target)


__all__ = ["PreparedSandboxFile", "SandboxCacheError", "TaskAttachmentCache"]
