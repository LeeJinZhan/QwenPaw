"""Task-local derived document chunks with opaque references and cursors."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import shutil
from typing import Callable
import uuid

from .schemas import ChunkPage, DocumentHandle, NormalizedChunk, NormalizedDocument


class DocumentStoreError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class _DocumentEntry:
    task_id: str
    path: Path
    title: str
    page_count: int | None
    chunk_count: int
    sha256: str
    expires_at: datetime


@dataclass(frozen=True)
class _CursorEntry:
    document_hash: str
    offset: int
    expires_at: datetime


class DocumentStore:
    def __init__(
        self,
        *,
        root: str | Path,
        process_start_key: bytes | None = None,
        max_document_bytes: int = 32 * 1024 * 1024,
        max_task_bytes: int = 64 * 1024 * 1024,
        ttl_seconds: int = 604_800,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        self.key = process_start_key or secrets.token_bytes(32)
        if len(self.key) < 32:
            raise ValueError("process_start_key must contain at least 32 bytes")
        self.max_document_bytes = max(1, int(max_document_bytes))
        self.max_task_bytes = max(self.max_document_bytes, int(max_task_bytes))
        self.ttl_seconds = max(60, min(int(ttl_seconds), 604_800))
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._documents: dict[str, _DocumentEntry] = {}
        self._cursors: dict[str, _CursorEntry] = {}

    def write(self, source, document: NormalizedDocument) -> DocumentHandle:
        now = _utc(self.clock())
        expiry = min(_utc(source.expires_at), now + timedelta(seconds=self.ttl_seconds))
        if expiry <= now:
            raise DocumentStoreError("DOCUMENT_REF_EXPIRED", "Document source expired")
        task_root_candidate = self.root / source.task_id
        source_path_candidate = Path(source.path)
        if task_root_candidate.is_symlink() or source_path_candidate.is_symlink():
            raise DocumentStoreError(
                "FILE_ACCESS_DENIED", "Document source is outside task scope"
            )
        task_root = task_root_candidate.resolve(strict=True)
        source_path = source_path_candidate.resolve(strict=True)
        if task_root.parent != self.root or not source_path.is_relative_to(task_root):
            raise DocumentStoreError(
                "FILE_ACCESS_DENIED", "Document source is outside task scope"
            )
        payload = b"".join(
            (json.dumps(asdict(chunk), ensure_ascii=False) + "\n").encode("utf-8")
            for chunk in document.chunks
        )
        result_size = max(len(payload), len(document.markdown.encode("utf-8")))
        if result_size > self.max_document_bytes:
            raise DocumentStoreError(
                "DOCUMENT_RESULT_TOO_LARGE",
                "Normalized document exceeds its size limit",
            )
        derived_root = task_root / ".mineru"
        if derived_root.is_symlink():
            raise DocumentStoreError(
                "FILE_ACCESS_DENIED", "Derived document root is invalid"
            )
        derived_root.mkdir(mode=0o700, exist_ok=True)
        os.chmod(derived_root, 0o700)
        task_bytes = sum(
            item.stat().st_size
            for item in derived_root.glob("*.chunks.jsonl")
            if item.is_file() and not item.is_symlink()
        )
        if task_bytes + len(payload) > self.max_task_bytes:
            raise DocumentStoreError(
                "DOCUMENT_RESULT_TOO_LARGE",
                "Task document results exceed their size limit",
            )
        nonce = secrets.token_bytes(32)
        document_hash = hashlib.sha256(nonce).hexdigest()
        target = derived_root / f"{document_hash}.chunks.jsonl"
        temporary = derived_root / f".{document_hash}.{uuid.uuid4().hex}.part"
        try:
            with temporary.open("xb") as handle:
                os.chmod(temporary, 0o600)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            os.chmod(target, 0o600)
        except BaseException:
            temporary.unlink(missing_ok=True)
            target.unlink(missing_ok=True)
            raise
        entry = _DocumentEntry(
            task_id=source.task_id,
            path=target,
            title=document.title,
            page_count=document.page_count,
            chunk_count=len(document.chunks),
            sha256=hashlib.sha256(payload).hexdigest(),
            expires_at=expiry,
        )
        self._documents[document_hash] = entry
        document_ref = self._token("dr1", nonce)
        return DocumentHandle(
            document_ref=document_ref,
            path=target,
            title=entry.title,
            page_count=entry.page_count,
            chunk_count=entry.chunk_count,
        )

    def read_chunks(
        self,
        document_ref: str,
        *,
        cursor: str | None,
        limit: int,
    ) -> ChunkPage:
        if not 1 <= int(limit) <= 10:
            raise DocumentStoreError("FILE_REF_INVALID", "Chunk limit is invalid")
        document_hash = self._token_hash("dr1", document_ref)
        entry = self._documents.get(document_hash)
        if entry is None or entry.expires_at <= _utc(self.clock()):
            self._expire_document(document_hash)
            raise DocumentStoreError(
                "DOCUMENT_REF_EXPIRED", "Document reference expired"
            )
        path = entry.path
        if path.is_symlink() or not path.is_file():
            self._expire_document(document_hash)
            raise DocumentStoreError(
                "DOCUMENT_REF_EXPIRED", "Document reference expired"
            )
        body = path.read_bytes()
        if len(body) > self.max_document_bytes or not hmac.compare_digest(
            hashlib.sha256(body).hexdigest(),
            entry.sha256,
        ):
            raise DocumentStoreError(
                "FILE_REF_INVALID", "Document result integrity failed"
            )
        offset = 0
        if cursor:
            cursor_hash = self._token_hash("cur1", cursor)
            cursor_entry = self._cursors.pop(cursor_hash, None)
            if (
                cursor_entry is None
                or cursor_entry.document_hash != document_hash
                or cursor_entry.expires_at <= _utc(self.clock())
            ):
                raise DocumentStoreError(
                    "DOCUMENT_REF_EXPIRED", "Document cursor expired"
                )
            offset = cursor_entry.offset
        chunks = tuple(
            NormalizedChunk(**json.loads(line))
            for line in body.decode("utf-8").splitlines()
            if line
        )
        page_chunks = chunks[offset : offset + int(limit)]
        next_offset = offset + len(page_chunks)
        has_more = next_offset < len(chunks)
        next_cursor = None
        if has_more:
            cursor_nonce = secrets.token_bytes(32)
            self._cursors[hashlib.sha256(cursor_nonce).hexdigest()] = _CursorEntry(
                document_hash=document_hash,
                offset=next_offset,
                expires_at=entry.expires_at,
            )
            next_cursor = self._token("cur1", cursor_nonce)
        return ChunkPage(
            document_ref=document_ref,
            chunks=page_chunks,
            next_cursor=next_cursor,
            has_more=has_more,
        )

    def delete_task(self, task_id: str) -> None:
        normalized = str(task_id or "").strip()
        for document_hash, entry in list(self._documents.items()):
            if entry.task_id == normalized:
                self._expire_document(document_hash)
        task_root = (self.root / normalized).resolve(strict=False)
        derived_root = task_root / ".mineru"
        if (
            task_root.parent == self.root
            and derived_root.exists()
            and not derived_root.is_symlink()
        ):
            shutil.rmtree(derived_root)

    def purge_expired(self) -> int:
        now = _utc(self.clock())
        expired = [
            key for key, item in self._documents.items() if item.expires_at <= now
        ]
        for key in expired:
            self._expire_document(key)
        self._cursors = {
            key: value for key, value in self._cursors.items() if value.expires_at > now
        }
        return len(expired)

    def clear_all(self) -> None:
        for document_hash in list(self._documents):
            self._expire_document(document_hash)
        self._cursors.clear()
        if not self.root.is_dir() or self.root.is_symlink():
            return
        for task_root in self.root.iterdir():
            derived_root = task_root / ".mineru"
            if (
                task_root.is_dir()
                and not task_root.is_symlink()
                and derived_root.is_dir()
                and not derived_root.is_symlink()
            ):
                shutil.rmtree(derived_root)

    def _expire_document(self, document_hash: str) -> None:
        entry = self._documents.pop(document_hash, None)
        if entry is not None:
            entry.path.unlink(missing_ok=True)
        self._cursors = {
            key: value
            for key, value in self._cursors.items()
            if value.document_hash != document_hash
        }

    def _token(self, prefix: str, nonce: bytes) -> str:
        mac = hmac.new(
            self.key, prefix.encode() + b"\0" + nonce, hashlib.sha256
        ).digest()
        return f"{prefix}_{nonce.hex()}_{mac.hex()}"

    def _token_hash(self, prefix: str, token: str) -> str:
        parts = str(token or "").split("_")
        if len(parts) != 3 or parts[0] != prefix:
            raise DocumentStoreError(
                "FILE_REF_INVALID", "Document reference is invalid"
            )
        try:
            nonce = bytes.fromhex(parts[1])
            supplied = bytes.fromhex(parts[2])
        except ValueError as exc:
            raise DocumentStoreError(
                "FILE_REF_INVALID", "Document reference is invalid"
            ) from exc
        expected = hmac.new(
            self.key, prefix.encode() + b"\0" + nonce, hashlib.sha256
        ).digest()
        if len(nonce) != 32 or not hmac.compare_digest(supplied, expected):
            raise DocumentStoreError(
                "FILE_REF_INVALID", "Document reference is invalid"
            )
        return hashlib.sha256(nonce).hexdigest()


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(timezone.utc)


__all__ = ["DocumentStore", "DocumentStoreError"]
