# -*- coding: utf-8 -*-
"""Tests for Runtime-authorized sandbox object access."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
import hashlib
import importlib
import json
import os
import stat
import threading
import time

import pytest

runtime_sandbox_oss_module = importlib.import_module(
    "qwenpaw.agents.tools.runtime_sandbox_oss",
)


def test_sandboxed_oss_client_unwraps_runtime_authorize_envelope(monkeypatch) -> None:
    captured: dict[str, object] = {}
    locator = {
        "file_id": "file_001",
        "storage_provider": "local",
        "object_key": "runtime/u001/file_001.md",
        "content_type": "text/markdown",
        "size_bytes": 7,
    }

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return json.dumps({"data": locator, "trace_id": "trace_001"}).encode(
                "utf-8",
            )

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["headers"] = dict(request.header_items())
        return FakeResponse()

    monkeypatch.setattr(runtime_sandbox_oss_module.urllib.request, "urlopen", fake_urlopen)
    client = runtime_sandbox_oss_module.SandboxedOssClient(
        runtime_base_url="http://runtime.local",
        service_token="service-token",
        timeout_seconds=3,
    )

    result = client.authorize_file("file_001", {"context_id": "ctx_001"})

    assert result == locator
    assert captured["url"] == "http://runtime.local/runtime/internal/sandbox/attachments/authorize"
    assert captured["timeout"] == 3
    assert captured["headers"]["Authorization"] == "Bearer service-token"


def test_task_attachment_cache_downloads_once_per_task(tmp_path) -> None:
    class FakeSandboxedOssClient:
        def __init__(self):
            self.authorize_calls: list[tuple[str, dict]] = []
            self.read_calls: list[dict] = []

        def authorize_file(self, file_id: str, sandbox_context: dict):
            self.authorize_calls.append((file_id, dict(sandbox_context)))
            return {
                "file_id": file_id,
                "storage_provider": "local",
                "object_key": f"runtime/{file_id}",
                "content_type": "text/plain",
                "size_bytes": 7,
                "original_name": "customer.txt",
                "expires_at": "2026-07-09T12:00:00+08:00",
            }

        def read_authorized_locator(self, locator: dict):
            self.read_calls.append(dict(locator))
            return runtime_sandbox_oss_module.SandboxedObjectContent(
                content=b"cached\n",
                content_type=str(locator["content_type"]),
                size_bytes=7,
            )

    client = FakeSandboxedOssClient()
    cache = runtime_sandbox_oss_module.TaskAttachmentCache(
        root=tmp_path,
        ttl_seconds=600,
    )
    sandbox_context = {"task_id": "task_001", "context_id": "ctx_001"}

    first = cache.prepare_file("file_001", sandbox_context, client=client)
    second = cache.prepare_file("file_001", sandbox_context, client=client)

    assert first.local_path == second.local_path
    assert first.local_path.read_bytes() == b"cached\n"
    assert ".part." not in first.local_path.name
    assert client.authorize_calls == [("file_001", sandbox_context)]
    assert len(client.read_calls) == 1


def test_task_attachment_cache_reauthorizes_when_context_changes(tmp_path) -> None:
    class FakeSandboxedOssClient:
        def __init__(self):
            self.authorize_calls: list[tuple[str, dict]] = []
            self.read_calls: list[dict] = []

        def authorize_file(self, file_id: str, sandbox_context: dict):
            self.authorize_calls.append((file_id, dict(sandbox_context)))
            return {
                "file_id": file_id,
                "storage_provider": "local",
                "object_key": f"runtime/{sandbox_context['context_id']}/{file_id}",
                "content_type": "text/plain",
                "size_bytes": 7,
                "original_name": "customer.txt",
            }

        def read_authorized_locator(self, locator: dict):
            self.read_calls.append(dict(locator))
            return runtime_sandbox_oss_module.SandboxedObjectContent(
                content=str(locator["object_key"]).encode("utf-8"),
                content_type=str(locator["content_type"]),
                size_bytes=7,
            )

    client = FakeSandboxedOssClient()
    cache = runtime_sandbox_oss_module.TaskAttachmentCache(
        root=tmp_path,
        ttl_seconds=600,
    )

    first = cache.prepare_file(
        "file_001",
        {"task_id": "task_001", "context_id": "ctx_001"},
        client=client,
    )
    second = cache.prepare_file(
        "file_001",
        {"task_id": "task_001", "context_id": "ctx_002"},
        client=client,
    )

    assert first.local_path != second.local_path
    assert "ctx_001" in first.local_path.parts
    assert "ctx_002" in second.local_path.parts
    assert [call[1]["context_id"] for call in client.authorize_calls] == [
        "ctx_001",
        "ctx_002",
    ]
    assert len(client.read_calls) == 2


def test_task_attachment_cache_rejects_oversized_locator_before_read(tmp_path, monkeypatch) -> None:
    class OversizedSandboxedOssClient:
        def __init__(self):
            self.read_called = False

        def authorize_file(self, file_id: str, sandbox_context: dict):
            return {
                "file_id": file_id,
                "storage_provider": "local",
                "object_key": f"runtime/{file_id}",
                "content_type": "text/plain",
                "size_bytes": 10,
                "original_name": "large.txt",
            }

        def read_authorized_locator(self, _locator: dict):
            self.read_called = True
            return runtime_sandbox_oss_module.SandboxedObjectContent(
                content=b"too large",
                content_type="text/plain",
                size_bytes=9,
            )

    monkeypatch.setenv("QWENPAW_TASK_FILE_MAX_BYTES", "4")
    client = OversizedSandboxedOssClient()
    cache = runtime_sandbox_oss_module.TaskAttachmentCache(
        root=tmp_path,
        ttl_seconds=600,
    )

    with pytest.raises(RuntimeError, match="too large"):
        cache.prepare_file(
            "file_001",
            {"task_id": "task_001", "context_id": "ctx_001"},
            client=client,
        )

    assert client.read_called is False


def test_task_attachment_cache_concurrent_duplicate_prepare_downloads_once(tmp_path) -> None:
    class FakeSandboxedOssClient:
        def __init__(self):
            self.authorize_calls: list[tuple[str, dict]] = []
            self.read_calls: list[dict] = []
            self._lock = threading.Lock()
            self.first_read_started = threading.Event()
            self.allow_reads_to_finish = threading.Event()

        def authorize_file(self, file_id: str, sandbox_context: dict):
            with self._lock:
                self.authorize_calls.append((file_id, dict(sandbox_context)))
            return {
                "file_id": file_id,
                "storage_provider": "local",
                "object_key": f"runtime/{file_id}",
                "content_type": "text/plain",
                "size_bytes": 7,
                "original_name": "customer.txt",
            }

        def read_authorized_locator(self, locator: dict):
            with self._lock:
                self.read_calls.append(dict(locator))
            self.first_read_started.set()
            assert self.allow_reads_to_finish.wait(timeout=2)
            return runtime_sandbox_oss_module.SandboxedObjectContent(
                content=b"cached\n",
                content_type=str(locator["content_type"]),
                size_bytes=7,
            )

    client = FakeSandboxedOssClient()
    cache = runtime_sandbox_oss_module.TaskAttachmentCache(
        root=tmp_path,
        ttl_seconds=600,
    )
    sandbox_context = {"task_id": "task_001", "context_id": "ctx_001"}

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(
            cache.prepare_file,
            "file_001",
            sandbox_context,
            client=client,
        )
        assert client.first_read_started.wait(timeout=2)
        second_future = executor.submit(
            cache.prepare_file,
            "file_001",
            sandbox_context,
            client=client,
        )
        time.sleep(0.05)
        client.allow_reads_to_finish.set()
        first = first_future.result(timeout=2)
        second = second_future.result(timeout=2)

    assert first.local_path == second.local_path
    assert client.authorize_calls == [("file_001", sandbox_context)]
    assert len(client.read_calls) == 1


def test_task_attachment_cache_cleanup_removes_failed_download_lock(tmp_path) -> None:
    class FailingSandboxedOssClient:
        def authorize_file(self, file_id: str, sandbox_context: dict):
            return {
                "file_id": file_id,
                "storage_provider": "local",
                "object_key": f"runtime/{file_id}",
                "content_type": "text/plain",
                "size_bytes": 7,
                "original_name": "customer.txt",
            }

        def read_authorized_locator(self, _locator: dict):
            raise RuntimeError("download failed")

    cache = runtime_sandbox_oss_module.TaskAttachmentCache(
        root=tmp_path,
        ttl_seconds=600,
    )

    with pytest.raises(RuntimeError, match="download failed"):
        cache.prepare_file(
            "file_001",
            {"task_id": "task_001", "context_id": "ctx_001"},
            client=FailingSandboxedOssClient(),
        )

    cache._download_locks[("task_001", "ctx_001", "file_001")] = threading.Lock()
    assert ("task_001", "ctx_001", "file_001") in cache._download_locks
    assert not any(key[0] == "task_001" for key in cache._prepared)

    cache.cleanup_task("task_001")

    assert not any(key[0] == "task_001" for key in cache._download_locks)
    assert not any(key[0] == "task_001" for key in cache._prepared)


def test_task_attachment_cache_failed_prepare_does_not_retain_download_lock(tmp_path) -> None:
    class FailingSandboxedOssClient:
        def authorize_file(self, file_id: str, sandbox_context: dict):
            return {
                "file_id": file_id,
                "storage_provider": "local",
                "object_key": f"runtime/{file_id}",
                "content_type": "text/plain",
                "size_bytes": 7,
                "original_name": "customer.txt",
            }

        def read_authorized_locator(self, _locator: dict):
            raise RuntimeError("download failed")

    cache = runtime_sandbox_oss_module.TaskAttachmentCache(
        root=tmp_path,
        ttl_seconds=600,
    )

    with pytest.raises(RuntimeError, match="download failed"):
        cache.prepare_file(
            "file_001",
            {"task_id": "task_001", "context_id": "ctx_001"},
            client=FailingSandboxedOssClient(),
        )

    assert not any(key[0] == "task_001" for key in cache._download_locks)
    assert not any(key[0] == "task_001" for key in cache._prepared)


def test_task_attachment_cache_cleanup_waits_for_active_download(tmp_path) -> None:
    class BlockingSandboxedOssClient:
        def __init__(self):
            self.read_started = threading.Event()
            self.release_read = threading.Event()

        def authorize_file(self, file_id: str, sandbox_context: dict):
            return {
                "file_id": file_id,
                "storage_provider": "local",
                "object_key": f"runtime/{file_id}",
                "content_type": "text/plain",
                "size_bytes": 7,
                "original_name": "customer.txt",
            }

        def read_authorized_locator(self, locator: dict):
            self.read_started.set()
            assert self.release_read.wait(timeout=2)
            return runtime_sandbox_oss_module.SandboxedObjectContent(
                content=b"cached\n",
                content_type=str(locator["content_type"]),
                size_bytes=7,
            )

    client = BlockingSandboxedOssClient()
    cache = runtime_sandbox_oss_module.TaskAttachmentCache(
        root=tmp_path,
        ttl_seconds=600,
    )
    sandbox_context = {"task_id": "task_001", "context_id": "ctx_001"}

    with ThreadPoolExecutor(max_workers=2) as executor:
        prepare_future = executor.submit(
            cache.prepare_file,
            "file_001",
            sandbox_context,
            client=client,
        )
        assert client.read_started.wait(timeout=2)
        cleanup_future = executor.submit(cache.cleanup_task, "task_001")
        with pytest.raises(FutureTimeoutError):
            cleanup_future.result(timeout=0.05)

        client.release_read.set()
        prepared = prepare_future.result(timeout=2)
        cleanup_future.result(timeout=2)

    assert not prepared.local_path.exists()
    assert not any(key[0] == "task_001" for key in cache._prepared)
    assert not any(key[0] == "task_001" for key in cache._download_locks)


def test_task_attachment_cache_isolated_by_task(tmp_path) -> None:
    class FakeSandboxedOssClient:
        def authorize_file(self, file_id: str, sandbox_context: dict):
            return {
                "file_id": file_id,
                "storage_provider": "local",
                "object_key": f"runtime/{sandbox_context['task_id']}/{file_id}",
                "content_type": "text/plain",
                "size_bytes": 4,
                "original_name": "shared.txt",
            }

        def read_authorized_locator(self, locator: dict):
            content = str(locator["object_key"]).encode("utf-8")
            return runtime_sandbox_oss_module.SandboxedObjectContent(
                content=content,
                content_type=str(locator["content_type"]),
                size_bytes=len(content),
            )

    cache = runtime_sandbox_oss_module.TaskAttachmentCache(
        root=tmp_path,
        ttl_seconds=600,
    )

    first = cache.prepare_file(
        "file_001",
        {"task_id": "task_001", "context_id": "ctx_001"},
        client=FakeSandboxedOssClient(),
    )
    second = cache.prepare_file(
        "file_001",
        {"task_id": "task_002", "context_id": "ctx_002"},
        client=FakeSandboxedOssClient(),
    )

    assert first.local_path != second.local_path
    assert "task_001" in first.local_path.parts
    assert "task_002" in second.local_path.parts


def test_task_attachment_cache_sweep_expired_removes_persisted_marker_from_new_instance(tmp_path) -> None:
    class FakeSandboxedOssClient:
        def authorize_file(self, file_id: str, sandbox_context: dict):
            return {
                "file_id": file_id,
                "storage_provider": "local",
                "object_key": f"runtime/{file_id}",
                "content_type": "text/plain",
                "size_bytes": 7,
                "original_name": "customer.txt",
            }

        def read_authorized_locator(self, locator: dict):
            return runtime_sandbox_oss_module.SandboxedObjectContent(
                content=b"cached\n",
                content_type=str(locator["content_type"]),
                size_bytes=7,
            )

    first_cache = runtime_sandbox_oss_module.TaskAttachmentCache(
        root=tmp_path,
        ttl_seconds=1,
    )
    prepared = first_cache.prepare_file(
        "file_001",
        {"task_id": "task_001", "context_id": "ctx_001"},
        client=FakeSandboxedOssClient(),
    )
    task_root = prepared.local_path.parents[4]
    assert task_root.is_dir()

    second_cache = runtime_sandbox_oss_module.TaskAttachmentCache(
        root=tmp_path,
        ttl_seconds=600,
    )
    second_cache.sweep_expired(now_epoch=time.time() + 2)

    assert not task_root.exists()


def test_task_attachment_cache_sweep_ignores_invalid_binary_marker(tmp_path) -> None:
    marker_path = tmp_path / "shard" / "31" / "task" / "task_001" / ".task-marker.json"
    marker_path.parent.mkdir(parents=True)
    marker_path.write_bytes(b"\xff" * 32)

    cache = runtime_sandbox_oss_module.TaskAttachmentCache(
        root=tmp_path,
        ttl_seconds=600,
    )

    cache.sweep_expired(now_epoch=time.time() + 999)

    assert marker_path.exists()


def test_task_attachment_cache_reserves_marker_filename(tmp_path) -> None:
    class FakeSandboxedOssClient:
        def authorize_file(self, file_id: str, sandbox_context: dict):
            return {
                "file_id": file_id,
                "storage_provider": "local",
                "object_key": f"runtime/{file_id}",
                "content_type": "text/plain",
                "size_bytes": 7,
                "original_name": ".task-marker.json",
            }

        def read_authorized_locator(self, locator: dict):
            return runtime_sandbox_oss_module.SandboxedObjectContent(
                content=b"cached\n",
                content_type=str(locator["content_type"]),
                size_bytes=7,
            )

    cache = runtime_sandbox_oss_module.TaskAttachmentCache(
        root=tmp_path,
        ttl_seconds=600,
    )

    prepared = cache.prepare_file(
        "file_001",
        {"task_id": "task_001", "context_id": "ctx_001"},
        client=FakeSandboxedOssClient(),
    )

    assert prepared.local_path.name == "attachment"
    assert prepared.original_name == "attachment"


def test_task_attachment_cache_writes_private_dirs_and_files(tmp_path) -> None:
    class FakeSandboxedOssClient:
        def authorize_file(self, file_id: str, sandbox_context: dict):
            return {
                "file_id": file_id,
                "storage_provider": "local",
                "object_key": f"runtime/{file_id}",
                "content_type": "text/plain",
                "size_bytes": 7,
                "original_name": "customer.txt",
            }

        def read_authorized_locator(self, locator: dict):
            return runtime_sandbox_oss_module.SandboxedObjectContent(
                content=b"cached\n",
                content_type=str(locator["content_type"]),
                size_bytes=7,
            )

    cache = runtime_sandbox_oss_module.TaskAttachmentCache(
        root=tmp_path,
        ttl_seconds=600,
    )

    prepared = cache.prepare_file(
        "file_001",
        {"task_id": "task_001", "context_id": "ctx_001"},
        client=FakeSandboxedOssClient(),
    )
    task_root = prepared.local_path.parents[4]

    assert stat.S_IMODE(prepared.local_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(task_root.stat().st_mode) == 0o700
    assert stat.S_IMODE(prepared.local_path.parent.stat().st_mode) == 0o700


def test_task_attachment_cache_uses_private_creation_modes(tmp_path, monkeypatch) -> None:
    mkdir_modes: list[int] = []
    open_modes: list[int] = []
    real_mkdir = os.mkdir
    real_open = os.open

    def fake_mkdir(path, mode=0o777, *, dir_fd=None):
        mkdir_modes.append(mode)
        if dir_fd is not None:
            return real_mkdir(path, mode, dir_fd=dir_fd)
        return real_mkdir(path, mode)

    def fake_open(path, flags, mode=0o777, *, dir_fd=None):
        open_modes.append(mode)
        if dir_fd is not None:
            return real_open(path, flags, mode, dir_fd=dir_fd)
        return real_open(path, flags, mode)

    class FakeSandboxedOssClient:
        def authorize_file(self, file_id: str, sandbox_context: dict):
            return {
                "file_id": file_id,
                "storage_provider": "local",
                "object_key": f"runtime/{file_id}",
                "content_type": "text/plain",
                "size_bytes": 7,
                "original_name": "customer.txt",
            }

        def read_authorized_locator(self, locator: dict):
            return runtime_sandbox_oss_module.SandboxedObjectContent(
                content=b"cached\n",
                content_type=str(locator["content_type"]),
                size_bytes=7,
            )

    monkeypatch.setattr(runtime_sandbox_oss_module.os, "mkdir", fake_mkdir)
    monkeypatch.setattr(runtime_sandbox_oss_module.os, "open", fake_open)
    cache = runtime_sandbox_oss_module.TaskAttachmentCache(
        root=tmp_path / "cache",
        ttl_seconds=600,
    )

    cache.prepare_file(
        "file_001",
        {"task_id": "task_001", "context_id": "ctx_001"},
        client=FakeSandboxedOssClient(),
    )

    assert mkdir_modes
    assert all(mode == 0o700 for mode in mkdir_modes)
    assert open_modes
    assert all(mode == 0o600 for mode in open_modes)


def test_task_attachment_cache_cleanup_rejects_symlink_escape(tmp_path) -> None:
    cache_root = tmp_path / "cache"
    outside_root = tmp_path / "outside"
    task_id = "task_001"
    shard = hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:2]
    escaped_task_root = outside_root / "task" / task_id
    escaped_task_root.mkdir(parents=True)
    (escaped_task_root / "secret.txt").write_text("secret", encoding="utf-8")
    shard_parent = cache_root / "shard"
    shard_parent.mkdir(parents=True)
    (shard_parent / shard).symlink_to(outside_root, target_is_directory=True)

    cache = runtime_sandbox_oss_module.TaskAttachmentCache(
        root=cache_root,
        ttl_seconds=600,
    )

    with pytest.raises(RuntimeError, match="cache path is invalid"):
        cache.cleanup_task(task_id)

    assert escaped_task_root.exists()


def test_sandboxed_oss_client_rejects_non_regular_local_object(tmp_path, monkeypatch) -> None:
    object_root = tmp_path / "objects"
    object_root.mkdir()
    object_dir = object_root / "runtime" / "file_001"
    object_dir.mkdir(parents=True)
    monkeypatch.setenv("QWENPAW_LOCAL_OBJECT_ROOT", str(object_root))
    client = runtime_sandbox_oss_module.SandboxedOssClient(
        runtime_base_url="http://runtime.local",
        service_token="service-token",
    )

    with pytest.raises(RuntimeError, match="not a regular file"):
        client.read_authorized_locator(
            {
                "storage_provider": "local",
                "object_key": "runtime/file_001",
                "content_type": "text/plain",
                "size_bytes": 0,
            }
        )


def test_safe_filename_sanitizes_path_traversal_to_task_local_path(tmp_path) -> None:
    class FakeSandboxedOssClient:
        def authorize_file(self, file_id: str, sandbox_context: dict):
            return {
                "file_id": file_id,
                "storage_provider": "local",
                "object_key": "runtime/secret",
                "content_type": "text/plain",
                "size_bytes": 6,
                "original_name": "../../secret.txt",
            }

        def read_authorized_locator(self, locator: dict):
            return runtime_sandbox_oss_module.SandboxedObjectContent(
                content=b"secret",
                content_type=str(locator["content_type"]),
                size_bytes=6,
            )

    cache = runtime_sandbox_oss_module.TaskAttachmentCache(
        root=tmp_path,
        ttl_seconds=600,
    )

    prepared = cache.prepare_file(
        "file_001",
        {"task_id": "task_001", "context_id": "ctx_001"},
        client=FakeSandboxedOssClient(),
    )
    shard = hashlib.sha256(b"task_001").hexdigest()[:2]
    expected_path = (
        tmp_path
        / "shard"
        / shard
        / "task"
        / "task_001"
        / "files"
        / "file_001"
        / "contexts"
        / "ctx_001"
        / "secret.txt"
    ).resolve()

    assert prepared.local_path.name == "secret.txt"
    assert prepared.local_path == expected_path
    assert ".." not in prepared.local_path.parts
    assert prepared.local_path.is_relative_to(tmp_path)
    assert prepared.local_path.read_bytes() == b"secret"
