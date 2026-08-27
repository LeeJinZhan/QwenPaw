"""Opaque, task-scoped references for Runtime-authorized local files."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import os
from pathlib import Path
import re
import secrets
from typing import Callable

from .cache import PreparedSandboxFile

_TASK_ID = re.compile(r"[A-Za-z0-9_-]{1,160}")
_EXTENSION = re.compile(r"\.[A-Za-z0-9]{1,15}")


class FileRefError(RuntimeError):
    """Stable, non-sensitive file reference failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ResolvedTaskFile:
    task_id: str
    file_id: str
    path: Path
    media_type: str
    extension: str
    size_bytes: int
    sha256: str
    expires_at: datetime


class FileRefRegistry:
    """Issue unguessable capabilities without encoding paths or file names."""

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        process_start_key: bytes | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._root = Path(
            root
            or os.environ.get("QWENPAW_TASK_FILE_ROOT")
            or "/tmp/qwenpaw-runtime-task-files"
        ).expanduser().resolve()
        self._key = process_start_key or secrets.token_bytes(32)
        if len(self._key) < 32:
            raise ValueError("process_start_key must contain at least 32 bytes")
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._entries: dict[str, ResolvedTaskFile] = {}
        self._expired: set[str] = set()

    def issue(
        self,
        prepared: PreparedSandboxFile,
        *,
        expires_at: datetime,
    ) -> str:
        task_id = str(prepared.task_id or "").strip()
        if not _TASK_ID.fullmatch(task_id):
            raise FileRefError("FILE_ACCESS_DENIED", "Task file scope is invalid")
        expiry = _as_utc(expires_at)
        if expiry <= _as_utc(self._clock()):
            raise FileRefError("FILE_REF_EXPIRED", "File reference has expired")
        task_root_candidate = self._root / task_id
        original_path = Path(prepared.local_path)
        if task_root_candidate.is_symlink() or original_path.is_symlink():
            raise FileRefError("FILE_ACCESS_DENIED", "Task file is outside its scope")
        try:
            task_root = task_root_candidate.resolve(strict=True)
            path = original_path.resolve(strict=True)
        except OSError as exc:
            raise FileRefError(
                "FILE_ACCESS_DENIED",
                "Task file is outside its scope",
            ) from exc
        if (
            task_root.parent != self._root
            or not path.is_file()
            or not path.is_relative_to(task_root)
        ):
            raise FileRefError("FILE_ACCESS_DENIED", "Task file is outside its scope")
        size_bytes = path.stat().st_size
        if size_bytes != int(prepared.size_bytes):
            raise FileRefError("FILE_REF_INVALID", "Task file size changed")
        extension = path.suffix.lower()
        if not _EXTENSION.fullmatch(extension):
            raise FileRefError("FILE_TYPE_UNSUPPORTED", "Task file extension is unsupported")
        digest = _sha256_file(path)
        nonce = secrets.token_bytes(32)
        nonce_text = _encode(nonce)
        mac = hmac.new(self._key, b"fr1\0" + nonce, hashlib.sha256).digest()
        token = f"fr1_{nonce_text}_{_encode(mac)}"
        nonce_hash = hashlib.sha256(nonce).hexdigest()
        self._entries[nonce_hash] = ResolvedTaskFile(
            task_id=task_id,
            file_id=str(prepared.file_id),
            path=path,
            media_type=str(prepared.content_type or "application/octet-stream")[:128],
            extension=extension,
            size_bytes=size_bytes,
            sha256=digest,
            expires_at=expiry,
        )
        self._expired.discard(nonce_hash)
        return token

    def resolve(
        self,
        file_ref: str,
        *,
        expected_task_id: str | None = None,
    ) -> ResolvedTaskFile:
        nonce, nonce_hash = self._authenticate(file_ref)
        entry = self._entries.get(nonce_hash)
        if entry is None:
            code = "FILE_REF_EXPIRED" if nonce_hash in self._expired else "FILE_REF_INVALID"
            raise FileRefError(code, "File reference is unavailable")
        if expected_task_id is not None and entry.task_id != str(expected_task_id).strip():
            raise FileRefError("FILE_ACCESS_DENIED", "File reference belongs to another task")
        if entry.expires_at <= _as_utc(self._clock()):
            self._expire(nonce_hash)
            raise FileRefError("FILE_REF_EXPIRED", "File reference has expired")
        task_root = (self._root / entry.task_id).resolve(strict=False)
        try:
            path = entry.path.resolve(strict=True)
            valid_path = (
                not entry.path.is_symlink()
                and path.is_file()
                and path.is_relative_to(task_root)
            )
        except OSError:
            valid_path = False
            path = entry.path
        if not valid_path:
            raise FileRefError("FILE_REF_EXPIRED", "Task file is unavailable")
        if path.stat().st_size != entry.size_bytes or not hmac.compare_digest(
            _sha256_file(path),
            entry.sha256,
        ):
            raise FileRefError("FILE_REF_INVALID", "Task file integrity check failed")
        del nonce
        return entry

    def revoke_task(self, task_id: str) -> None:
        normalized = str(task_id or "").strip()
        for nonce_hash, entry in list(self._entries.items()):
            if entry.task_id == normalized:
                self._expire(nonce_hash)

    def purge_expired(self, now: datetime | None = None) -> int:
        cutoff = _as_utc(now or self._clock())
        expired = [
            nonce_hash
            for nonce_hash, entry in self._entries.items()
            if entry.expires_at <= cutoff
        ]
        for nonce_hash in expired:
            self._expire(nonce_hash)
        return len(expired)

    def _authenticate(self, file_ref: str) -> tuple[bytes, str]:
        parts = str(file_ref or "").split("_")
        if len(parts) != 3 or parts[0] != "fr1":
            raise FileRefError("FILE_REF_INVALID", "File reference is invalid")
        try:
            nonce = _decode(parts[1])
            supplied_mac = _decode(parts[2])
        except ValueError as exc:
            raise FileRefError("FILE_REF_INVALID", "File reference is invalid") from exc
        if len(nonce) != 32 or len(supplied_mac) != hashlib.sha256().digest_size:
            raise FileRefError("FILE_REF_INVALID", "File reference is invalid")
        expected_mac = hmac.new(self._key, b"fr1\0" + nonce, hashlib.sha256).digest()
        if not hmac.compare_digest(supplied_mac, expected_mac):
            raise FileRefError("FILE_REF_INVALID", "File reference is invalid")
        return nonce, hashlib.sha256(nonce).hexdigest()

    def _expire(self, nonce_hash: str) -> None:
        self._entries.pop(nonce_hash, None)
        self._expired.add(nonce_hash)


def _encode(value: bytes) -> str:
    return value.hex()


def _decode(value: str) -> bytes:
    try:
        return bytes.fromhex(value)
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid hexadecimal token segment") from exc


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(timezone.utc)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


_REGISTRY: FileRefRegistry | None = None


def get_file_ref_registry() -> FileRefRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = FileRefRegistry()
    return _REGISTRY


__all__ = [
    "FileRefError",
    "FileRefRegistry",
    "ResolvedTaskFile",
    "get_file_ref_registry",
]
