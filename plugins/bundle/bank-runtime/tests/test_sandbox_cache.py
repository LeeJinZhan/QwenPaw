from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sys

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from bank_runtime.sandbox.cache import SandboxCacheError, TaskAttachmentCache
from bank_runtime.sandbox.scope import SandboxRequestScope


class _Broker:
    def __init__(
        self,
        object_root: Path,
        *,
        extra=False,
        original_name: str | None = None,
        content_type: str = "text/plain",
    ) -> None:
        self.object_root = object_root
        self.extra = extra
        self.original_name = original_name
        self.content_type = content_type
        self.calls = []

    async def authorize_files(self, scope, file_ids, selection_records=None):
        self.calls.append((list(file_ids), list(selection_records or [])))
        authorized = []
        for file_id in file_ids:
            content = f"content:{file_id}".encode()
            path = self.object_root / file_id
            path.write_bytes(content)
            authorized.append(
                {
                    "file_id": file_id,
                    "storage_provider": "local",
                    "object_key": file_id,
                    "original_name": self.original_name or f"{file_id}.txt",
                    "content_type": self.content_type,
                    "size_bytes": len(content),
                    "content_hash": "sha256:" + hashlib.sha256(content).hexdigest(),
                    "expires_at": "2026-08-19T12:00:00+08:00",
                }
            )
        if self.extra:
            authorized.append({"file_id": "file_extra"})
        return {"authorized": authorized, "denied": []}

    def stream_locator(self, locator, write_chunk):
        path = (self.object_root / locator["object_key"]).resolve()
        assert path.parent == self.object_root.resolve()
        write_chunk(path.read_bytes())


def _scope() -> SandboxRequestScope:
    request = type(
        "Request",
        (),
        {
            "runtime_task_id": "task_001",
            "sandbox_context": {
                "context_id": "ctx_001",
                "task_id": "task_001",
                "signature": "signed",
            },
            "attachments_manifest": [
                {"file_id": "file_current", "source": "current_task"}
            ],
        },
    )()
    return SandboxRequestScope.from_request(request)


@pytest.mark.asyncio
async def test_cache_materializes_private_hash_bound_files_and_cleans_up(
    tmp_path,
) -> None:
    object_root = tmp_path / "objects"
    object_root.mkdir()
    cache = TaskAttachmentCache(tmp_path / "cache", max_files=3, max_total_bytes=1024)
    prepared = await cache.prepare_files(
        _scope(),
        ["file_current"],
        _Broker(object_root),
    )

    assert prepared[0].local_path.read_text() == "content:file_current"
    assert prepared[0].local_path.is_relative_to(tmp_path / "cache" / "task_001")
    assert os.stat(prepared[0].local_path).st_mode & 0o777 == 0o600
    assert os.stat(prepared[0].local_path.parent).st_mode & 0o777 == 0o700

    await cache.cleanup("task_001")
    assert not (tmp_path / "cache" / "task_001").exists()


@pytest.mark.asyncio
async def test_cache_preserves_extension_for_non_ascii_filename(tmp_path) -> None:
    object_root = tmp_path / "objects"
    object_root.mkdir()
    cache = TaskAttachmentCache(tmp_path / "cache")

    prepared = await cache.prepare_files(
        _scope(),
        ["file_current"],
        _Broker(
            object_root,
            original_name="通义灵码安装及使用指南.pptx",
            content_type=(
                "application/vnd.openxmlformats-officedocument."
                "presentationml.presentation"
            ),
        ),
    )

    assert prepared[0].original_name == "attachment.pptx"
    assert prepared[0].local_path.suffix == ".pptx"


@pytest.mark.asyncio
async def test_cache_rejects_runtime_authorization_set_injection(tmp_path) -> None:
    object_root = tmp_path / "objects"
    object_root.mkdir()
    cache = TaskAttachmentCache(tmp_path / "cache")
    with pytest.raises(SandboxCacheError):
        await cache.prepare_files(
            _scope(),
            ["file_current"],
            _Broker(object_root, extra=True),
        )


@pytest.mark.asyncio
async def test_cache_rejects_quota_before_committing_file(tmp_path) -> None:
    object_root = tmp_path / "objects"
    object_root.mkdir()
    cache = TaskAttachmentCache(tmp_path / "cache", max_files=3, max_total_bytes=4)
    with pytest.raises(SandboxCacheError):
        await cache.prepare_files(
            _scope(),
            ["file_current"],
            _Broker(object_root),
        )
    task_root = tmp_path / "cache" / "task_001"
    assert not list(task_root.glob("*.part-*")) if task_root.exists() else True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cache_kwargs", "second_file"),
    [
        ({"max_files": 1, "max_total_bytes": 1024}, "file_second"),
        ({"max_files": 3, "max_total_bytes": 30}, "file_second"),
    ],
)
async def test_cache_enforces_task_quota_across_separate_calls(
    tmp_path,
    cache_kwargs,
    second_file,
) -> None:
    object_root = tmp_path / "objects"
    object_root.mkdir()
    cache = TaskAttachmentCache(tmp_path / "cache", **cache_kwargs)
    broker = _Broker(object_root)

    await cache.prepare_files(_scope(), ["file_current"], broker)
    with pytest.raises(SandboxCacheError):
        await cache.prepare_files(_scope(), [second_file], broker)

    assert sorted(
        path.name for path in (tmp_path / "cache" / "task_001").iterdir()
    ) == ["file_current.txt"]
