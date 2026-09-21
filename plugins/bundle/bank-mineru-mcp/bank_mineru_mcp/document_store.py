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
        self.key = process_start_key or _load_key(self.root / ".bank-mineru-layout.key")
        self.max_document_bytes = max(1, int(max_document_bytes))
        self.max_task_bytes = max(self.max_document_bytes, int(max_task_bytes))
        self.ttl_seconds = max(60, min(int(ttl_seconds), 604_800))
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._documents: dict[str, _DocumentEntry] = {}
        self._verified: dict[str, tuple[int, int]] = {}
        self._recover()

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
        offsets_target = derived_root / f"{document_hash}.offsets.json"
        temporary = derived_root / f".{document_hash}.{uuid.uuid4().hex}.part"
        offsets = _line_offsets(payload)
        try:
            with temporary.open("xb") as handle:
                os.chmod(temporary, 0o600)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            os.chmod(target, 0o600)
            offsets_target.write_text(json.dumps(offsets), encoding="ascii")
            os.chmod(offsets_target, 0o600)
        except BaseException:
            temporary.unlink(missing_ok=True)
            target.unlink(missing_ok=True)
            offsets_target.unlink(missing_ok=True)
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
        stat = target.stat()
        self._verified[document_hash] = (stat.st_size, stat.st_mtime_ns)
        _write_manifest(target, entry, document_hash, offsets_target.name)
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
        stat = path.stat()
        stamp = (stat.st_size, stat.st_mtime_ns)
        if self._verified.get(document_hash) != stamp:
            body = path.read_bytes()
            if len(body) > self.max_document_bytes or not hmac.compare_digest(
                hashlib.sha256(body).hexdigest(),
                entry.sha256,
            ):
                raise DocumentStoreError(
                    "FILE_REF_INVALID", "Document result integrity failed"
                )
            self._verified[document_hash] = stamp
        offsets_path = path.with_name(path.name.replace(".chunks.jsonl", ".offsets.json"))
        try:
            offsets = json.loads(offsets_path.read_text(encoding="ascii"))
            total = len(offsets)
        except (OSError, json.JSONDecodeError):
            offsets = None
            total = entry.chunk_count
        offset = 0
        if cursor:
            offset = self._cursor_offset("cur1", cursor, document_hash)
        page_chunks = self._read_page(path, offsets, offset, int(limit), total)
        # Bound the serialized UTF-8 page, including metadata and cursor overhead.
        while page_chunks and len(json.dumps([asdict(c) for c in page_chunks], ensure_ascii=False, indent=2).encode("utf-8")) > 30000:
            page_chunks = page_chunks[:-1]
        if not page_chunks and offset < total:
            raise DocumentStoreError("DOCUMENT_RESULT_TOO_LARGE", "A document block exceeds the response budget")
        next_offset = offset + len(page_chunks)
        has_more = next_offset < total
        next_cursor = (
            self._cursor_token("cur1", document_hash, next_offset) if has_more else None
        )
        return ChunkPage(
            document_ref=document_ref,
            chunks=page_chunks,
            next_cursor=next_cursor,
            has_more=has_more,
            coverage=(next_offset, total),
        )

    def _read_page(self, path: Path, offsets, offset: int, limit: int, total: int):
        chunks = []
        if offsets is None:
            body = path.read_bytes()
            all_chunks = tuple(
                NormalizedChunk(**json.loads(line))
                for line in body.decode("utf-8").splitlines()
                if line
            )
            return all_chunks[offset : offset + limit]
        with path.open("rb") as handle:
            handle.seek(offsets[min(offset, len(offsets) - 1)])
            for position, line in enumerate(handle):
                if position + offset >= offset + limit:
                    break
                if position + offset >= total:
                    break
                if line.strip():
                    chunks.append(NormalizedChunk(**json.loads(line)))
        return tuple(chunks)

    def _recover(self) -> None:
        if not self.root.is_dir() or self.root.is_symlink():
            return
        for task_root in self.root.iterdir():
            derived = task_root / ".mineru"
            if not task_root.is_dir() or derived.is_symlink() or not derived.is_dir():
                continue
            for manifest in derived.glob("*.manifest.json"):
                try:
                    payload = json.loads(manifest.read_text(encoding="utf-8"))
                    expires = datetime.fromisoformat(payload["expires_at"])
                    if expires <= _utc(self.clock()):
                        continue
                    chunks_path = manifest.with_name(payload["chunks_file"])
                    if not chunks_path.is_file():
                        continue
                    self._documents[payload["document_hash"]] = _DocumentEntry(
                        task_id=payload["task_id"],
                        path=chunks_path,
                        title=payload["title"],
                        page_count=payload["page_count"],
                        chunk_count=payload["chunk_count"],
                        sha256=payload["sha256"],
                        expires_at=expires,
                    )
                except (OSError, ValueError, KeyError, json.JSONDecodeError):
                    continue

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
        return len(expired)

    def clear_all(self) -> None:
        for document_hash in list(self._documents):
            self._expire_document(document_hash)
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
        self._verified.pop(document_hash, None)
        if entry is not None:
            entry.path.unlink(missing_ok=True)
            entry.path.with_name(entry.path.name.replace(".chunks.jsonl", ".offsets.json")).unlink(
                missing_ok=True
            )
            entry.path.with_name(entry.path.name.replace(".chunks.jsonl", ".manifest.json")).unlink(
                missing_ok=True
            )

    def _cursor_token(self, prefix: str, document_hash: str, offset: int) -> str:
        message = f"{prefix}\0{document_hash}\0{offset}".encode()
        nonce = hmac.new(self.key, message, hashlib.sha256).digest()[:16]
        mac = hmac.new(self.key, message + nonce, hashlib.sha256).digest()
        return f"{prefix}_{offset}_{nonce.hex()}_{mac.hex()}"

    def _cursor_offset(self, prefix: str, cursor: str, document_hash: str) -> int:
        parts = str(cursor or "").split("_")
        if len(parts) != 4 or parts[0] != prefix:
            raise DocumentStoreError("FILE_REF_INVALID", "Document cursor is invalid")
        try:
            offset = int(parts[1])
            nonce = bytes.fromhex(parts[2])
            supplied = bytes.fromhex(parts[3])
        except ValueError as exc:
            raise DocumentStoreError(
                "FILE_REF_INVALID", "Document cursor is invalid"
            ) from exc
        message = f"{prefix}\0{document_hash}\0{offset}".encode()
        expected = hmac.new(self.key, message + nonce, hashlib.sha256).digest()
        if not hmac.compare_digest(supplied, expected):
            raise DocumentStoreError("FILE_REF_INVALID", "Document cursor is invalid")
        return max(0, offset)

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


def _load_key(path: Path) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        os.chmod(path, 0o600)
        token = path.read_bytes()
        if len(token) >= 32:
            return token
        path.unlink()
    token = secrets.token_bytes(32)
    with path.open("xb") as handle:
        os.chmod(path, 0o600)
        handle.write(token)
    return token


def _line_offsets(payload: bytes) -> list[int]:
    offsets = []
    position = 0
    for line in payload.splitlines(keepends=True):
        if line.strip():
            offsets.append(position)
        position += len(line)
    return offsets


def _write_manifest(path: Path, entry: _DocumentEntry, document_hash: str, offsets_file: str) -> None:
    manifest = {
        "document_hash": document_hash,
        "task_id": entry.task_id,
        "title": entry.title,
        "page_count": entry.page_count,
        "chunk_count": entry.chunk_count,
        "sha256": entry.sha256,
        "expires_at": entry.expires_at.isoformat(),
        "chunks_file": path.name,
        "offsets_file": offsets_file,
    }
    target = path.with_name(path.name.replace(".chunks.jsonl", ".manifest.json"))
    target.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    os.chmod(target, 0o600)


__all__ = ["DocumentStore", "DocumentStoreError"]
