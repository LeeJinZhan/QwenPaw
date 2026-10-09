"""Fixed-endpoint Runtime file broker; locators stay below the model boundary."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx
from qwenpaw.exceptions import AgentRuntimeErrorException

from .scope import SandboxRequestScope


class SandboxBrokerError(RuntimeError):
    """Runtime file broker transport or response failed."""


class AttachmentStorageError(AgentRuntimeErrorException):
    """Source bytes are unavailable; keep storage details in the cause only."""

    def __init__(self) -> None:
        super().__init__(error_code="FILE_ACCESS_DENIED",
                         message="这份文件当前无法使用。请重新上传或选择可用文件，再发送问题。",
                         details={})


class RuntimeFileBroker:
    def __init__(self, base_url: str, token: str | None = None) -> None:
        self.base_url = str(base_url or "").rstrip("/")
        self.token = str(token or os.environ.get("QWENPAW_SERVICE_TOKEN") or "").strip()
        if not self.base_url or not self.token:
            raise SandboxBrokerError("Runtime file broker is unavailable")

    async def search(
        self,
        scope: SandboxRequestScope,
        *,
        query: str,
        content_types: list[str],
        extensions: list[str],
        sources: list[str],
        limit: int,
    ) -> list[dict[str, Any]]:
        data = await self._post(
            "/runtime/internal/sandbox/files/search",
            {
                "sandbox_context": dict(scope.sandbox_context),
                "query": query,
                "content_types": content_types,
                "extensions": extensions,
                "sources": sources,
                "limit": limit,
                "include_current_task": False,
            },
        )
        files = data.get("files")
        if not isinstance(files, list):
            raise SandboxBrokerError("Runtime file search response is invalid")
        return files

    async def authorize_files(
        self,
        scope: SandboxRequestScope,
        file_ids: list[str],
        selection_records: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "sandbox_context": dict(scope.sandbox_context),
            "file_ids": list(file_ids),
        }
        if selection_records:
            payload["selection_records"] = list(selection_records)
        data = await self._post(
            "/runtime/internal/sandbox/attachments/batch-authorize",
            payload,
        )
        if not isinstance(data.get("authorized"), list) or not isinstance(
            data.get("denied"), list
        ):
            raise SandboxBrokerError("Runtime attachment authorization is invalid")
        return data

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(
                timeout=30,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                response = await client.post(
                    f"{self.base_url}{path}",
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {self.token}",
                        "Content-Type": "application/json",
                    },
                )
        except httpx.HTTPError as exc:
            raise SandboxBrokerError("Runtime file broker request failed") from exc
        if response.status_code >= 400:
            raise SandboxBrokerError("Runtime file broker request rejected")
        try:
            envelope = response.json()
        except ValueError as exc:
            raise SandboxBrokerError("Runtime file broker response is invalid") from exc
        data = envelope.get("data") if isinstance(envelope, dict) else None
        if not isinstance(data, dict):
            raise SandboxBrokerError("Runtime file broker response is invalid")
        return data

    async def validate_cached_files(self, scope, files):
        data = await self._post("/runtime/internal/sandbox/attachments/validate-materialized",
            {"sandbox_context": dict(scope.sandbox_context), "file_ids": [item.file_id for item in files]})
        verified = {item.get("file_id"): item for item in data.get("files", []) if isinstance(item, dict)}
        if len(verified) != len(files) or any(item.file_id not in verified
                or verified[item.file_id].get("content_hash") != item.sha256
                or verified[item.file_id].get("size_bytes") != item.size_bytes for item in files):
            raise SandboxBrokerError("Cached source authority or integrity changed")

    async def prepare_originals(self, scope, files):
        data = await self._post('/runtime/internal/sandbox/attachments/prepare-originals',
            {'sandbox_context': dict(scope.sandbox_context), 'file_ids': [item.file_id for item in files]})
        raw = data.get('files')
        if not isinstance(raw, list) or len(raw) != len(files):
            raise SandboxBrokerError('Original preparation response is invalid')
        expected = {item.file_id: item for item in files}
        safe, seen = [], set()
        import re
        for item in raw:
            if not isinstance(item, dict) or item.get('file_id') not in expected or item['file_id'] in seen:
                raise SandboxBrokerError('Original preparation response is invalid')
            original = expected[item['file_id']]
            path = item.get('container_path')
            if (item.get('content_hash') != original.sha256 or item.get('size_bytes') != original.size_bytes
                    or not isinstance(path, str) or not re.fullmatch(
                        '/workspace/input/' + re.escape(original.file_id) + '/' + re.escape(original.sha256) + r'(?:\.[a-z0-9]{1,15})?', path)):
                raise SandboxBrokerError('Original preparation integrity changed')
            seen.add(item['file_id'])
            safe.append({key: item.get(key) for key in ('file_id', 'display_name', 'content_type', 'size_bytes',
                                                      'content_hash', 'container_path')})
        return safe

    def stream_locator(self, locator: dict[str, Any], write_chunk: Any) -> None:
        provider = str(locator.get("storage_provider") or "").lower()
        object_key = _safe_object_key(locator.get("object_key"))
        if provider == "local":
            try:
                _stream_local(object_key, write_chunk)
            except (SandboxBrokerError, OSError) as exc:
                raise AttachmentStorageError() from exc
            return
        if provider == "oss":
            _stream_oss(locator, object_key, write_chunk)
            return
        raise SandboxBrokerError("Attachment storage provider is unsupported")


def _safe_object_key(value: Any) -> str:
    key = str(value or "")
    parts = key.split("/")
    if (
        not key
        or key != key.strip()
        or key.startswith("/")
        or "\\" in key
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise SandboxBrokerError("Attachment object key is invalid")
    return key


def _stream_local(object_key: str, write_chunk: Any) -> None:
    root_value = str(
        os.environ.get("QWENPAW_LOCAL_OBJECT_ROOT")
        or os.environ.get("RUNTIME_LOCAL_OBJECT_ROOT")
        or ""
    ).strip()
    if not root_value:
        raise SandboxBrokerError("Local object root is unavailable")
    root = Path(root_value).expanduser().resolve()
    target = (root / object_key).resolve()
    if root not in target.parents or not target.is_file() or target.is_symlink():
        raise SandboxBrokerError("Local attachment object is invalid")
    with target.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            write_chunk(chunk)


def _stream_oss(locator: dict[str, Any], object_key: str, write_chunk: Any) -> None:
    endpoint = str(os.environ.get("OSS_ENDPOINT") or "").strip()
    bucket_name = str(
        locator.get("bucket") or os.environ.get("OSS_BUCKET") or ""
    ).strip()
    access_key = str(os.environ.get("OSS_ACCESS_KEY_ID") or "").strip()
    secret = str(os.environ.get("OSS_ACCESS_KEY_SECRET") or "").strip()
    if not all((endpoint, bucket_name, access_key, secret)):
        raise SandboxBrokerError("OSS reader is unavailable")
    if "://" not in endpoint:
        endpoint = f"https://{endpoint}"
    try:
        import oss2
    except ImportError as exc:
        raise SandboxBrokerError("OSS reader dependency is unavailable") from exc
    try:
        stream = oss2.Bucket(
            oss2.Auth(access_key, secret), endpoint, bucket_name
        ).get_object(object_key)
        try:
            while chunk := stream.read(1024 * 1024):
                write_chunk(chunk)
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                close()
    except oss2.exceptions.OssError as exc:
        raise AttachmentStorageError() from exc


__all__ = ["RuntimeFileBroker", "SandboxBrokerError"]
