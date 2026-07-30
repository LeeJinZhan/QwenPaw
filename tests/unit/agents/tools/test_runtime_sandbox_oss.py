# -*- coding: utf-8 -*-
"""Tests for Runtime-authorized sandbox object access."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
import hashlib
import importlib
import json
import os
import stat
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from agentscope.message import Msg

runtime_sandbox_oss_module = importlib.import_module(
    "qwenpaw.agents.tools.runtime_sandbox_oss",
)


def test_qwenpaw_runtime_dependencies_include_oss_reader() -> None:
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10 compatibility
        import tomli as tomllib

    project_root = Path(__file__).resolve().parents[4]
    with (project_root / "pyproject.toml").open("rb") as pyproject_file:
        dependencies = tomllib.load(pyproject_file)["project"]["dependencies"]

    assert any(dependency.startswith("oss2") for dependency in dependencies)


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


def test_sandboxed_oss_client_batch_authorizes_once(monkeypatch) -> None:
    captured: dict[str, object] = {"calls": 0}
    authorized = [
        {
            "file_id": "file_a",
            "storage_provider": "local",
            "object_key": "runtime/file_a",
        },
        {
            "file_id": "file_b",
            "storage_provider": "local",
            "object_key": "runtime/file_b",
        },
    ]

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return json.dumps(
                {"data": {"authorized": authorized, "denied": []}},
            ).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["calls"] = int(captured["calls"]) + 1
        captured["url"] = request.full_url
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(runtime_sandbox_oss_module.urllib.request, "urlopen", fake_urlopen)
    client = runtime_sandbox_oss_module.SandboxedOssClient(
        runtime_base_url="http://runtime.local",
        service_token="service-token",
        timeout_seconds=3,
    )
    sandbox_context = {"task_id": "task_a", "context_id": "ctx_a"}

    result = client.authorize_files(
        ["file_a", "file_b", "file_a"],
        sandbox_context,
    )

    assert result == {"authorized": authorized, "denied": []}
    assert captured["calls"] == 1
    assert captured["url"] == (
        "http://runtime.local/runtime/internal/sandbox/attachments/batch-authorize"
    )
    assert captured["payload"] == {
        "sandbox_context": sandbox_context,
        "file_ids": ["file_a", "file_b"],
    }
    assert captured["timeout"] == 3


def test_sandboxed_oss_client_reads_only_requested_prefix(tmp_path, monkeypatch) -> None:
    local_path = tmp_path / "large.txt"
    local_path.write_bytes(b"0123456789" * 10)

    class FakeTaskAttachmentCache(runtime_sandbox_oss_module.TaskAttachmentCache):
        def __init__(self):
            super().__init__(root=tmp_path / "cache")

        def prepare_file(self, file_id: str, sandbox_context: dict, *, client):
            return runtime_sandbox_oss_module.PreparedSandboxFile(
                file_id=file_id,
                local_path=local_path,
                content_type="text/plain",
                size_bytes=100,
                original_name="large.txt",
                expires_at="2999-01-01T00:00:00+08:00",
            )

    monkeypatch.setattr(
        runtime_sandbox_oss_module,
        "_DEFAULT_TASK_ATTACHMENT_CACHE",
        FakeTaskAttachmentCache(),
    )
    client = runtime_sandbox_oss_module.SandboxedOssClient()

    content = client.read_file(
        "file_large",
        {"task_id": "task_a", "context_id": "ctx_a"},
        max_bytes=13,
    )

    assert content.content == b"0123456789012"
    assert content.size_bytes == 100


def test_sandboxed_oss_client_read_file_holds_direct_io_reservation(
    tmp_path,
    monkeypatch,
) -> None:
    local_path = tmp_path / "preview.txt"
    local_path.write_bytes(b"preview")
    cache = runtime_sandbox_oss_module.TaskAttachmentCache(root=tmp_path / "cache")
    observed: list[int] = []

    def fake_prepare_file(file_id: str, sandbox_context: dict, *, client):
        del sandbox_context, client
        observed.append(cache._io_reservations.get("task_preview", 0))
        return runtime_sandbox_oss_module.PreparedSandboxFile(
            file_id=file_id,
            local_path=local_path,
            content_type="text/plain",
            size_bytes=7,
            original_name="preview.txt",
            expires_at="",
        )

    cache.prepare_file = fake_prepare_file
    monkeypatch.setattr(
        runtime_sandbox_oss_module,
        "_DEFAULT_TASK_ATTACHMENT_CACHE",
        cache,
    )

    content = runtime_sandbox_oss_module.SandboxedOssClient().read_file(
        "file_preview",
        {"task_id": "task_preview", "context_id": "ctx_preview"},
        max_bytes=7,
    )

    assert content.content == b"preview"
    assert observed == [1]
    assert cache._io_reservations == {}


def test_task_attachment_cache_streams_large_file_to_private_atomic_target(
    tmp_path,
) -> None:
    chunk = b"x" * (64 * 1024)

    class StreamingClient:
        def authorize_file(self, file_id, _sandbox_context):
            return {
                "file_id": file_id,
                "storage_provider": "local",
                "object_key": f"runtime/{file_id}",
                "content_type": "application/octet-stream",
                "size_bytes": len(chunk) * 32,
                "original_name": "large.bin",
            }

        def stream_authorized_locator(self, _locator, write_chunk):
            for _ in range(32):
                write_chunk(chunk)
            return "application/octet-stream"

        def read_authorized_locator(self, _locator):
            raise AssertionError("downloads must not buffer the whole object")

    cache = runtime_sandbox_oss_module.TaskAttachmentCache(root=tmp_path)

    prepared = cache.prepare_file(
        "file_large",
        {"task_id": "task_large", "context_id": "ctx_large"},
        client=StreamingClient(),
    )

    assert prepared.size_bytes == len(chunk) * 32
    assert prepared.local_path.stat().st_size == len(chunk) * 32
    assert stat.S_IMODE(prepared.local_path.stat().st_mode) == 0o600
    assert not list(tmp_path.rglob("*.part.*"))


def test_prepare_file_reserves_io_before_ttl_sweep_and_releases(tmp_path) -> None:
    cache = runtime_sandbox_oss_module.TaskAttachmentCache(root=tmp_path)
    observed_reservations: list[int] = []

    def observe_sweep(*_args, **_kwargs) -> None:
        observed_reservations.append(
            getattr(cache, "_io_reservations", {}).get("task_reserved", 0),
        )

    class StreamingClient:
        def authorize_file(self, file_id, _sandbox_context):
            return {
                "file_id": file_id,
                "storage_provider": "local",
                "object_key": f"runtime/{file_id}",
                "content_type": "text/plain",
                "size_bytes": 4,
                "original_name": "reserved.txt",
            }

        def stream_authorized_locator(self, _locator, write_chunk):
            write_chunk(b"data")
            return "text/plain"

    cache.sweep_expired = observe_sweep

    prepared = cache.prepare_file(
        "file_reserved",
        {"task_id": "task_reserved", "context_id": "ctx_reserved"},
        client=StreamingClient(),
    )

    assert prepared.local_path.read_bytes() == b"data"
    assert observed_reservations == [1]
    assert getattr(cache, "_io_reservations", {}) == {}


def test_prepare_file_releases_io_reservation_on_error(tmp_path) -> None:
    cache = runtime_sandbox_oss_module.TaskAttachmentCache(root=tmp_path)

    class FailingClient:
        def authorize_file(self, *_args, **_kwargs):
            raise RuntimeError("authorization failed")

    with pytest.raises(RuntimeError, match="authorization failed"):
        cache.prepare_file(
            "file_failed",
            {"task_id": "task_failed", "context_id": "ctx_failed"},
            client=FailingClient(),
        )

    assert getattr(cache, "_io_reservations", {}) == {}


def test_cleanup_waits_for_io_reservation_then_allows_same_task_retry(
    tmp_path,
) -> None:
    cache = runtime_sandbox_oss_module.TaskAttachmentCache(root=tmp_path)
    assert hasattr(cache, "reserve_task_io")
    reservation = cache.reserve_task_io("task_retry")

    with ThreadPoolExecutor(max_workers=1) as executor:
        cleanup_future = executor.submit(cache.cleanup_task, "task_retry")
        with pytest.raises(FutureTimeoutError):
            cleanup_future.result(timeout=0.05)
        reservation.release()
        cleanup_future.result(timeout=2)

    class RetryClient:
        def authorize_file(self, file_id, _sandbox_context):
            return {
                "file_id": file_id,
                "storage_provider": "local",
                "object_key": f"runtime/{file_id}",
                "content_type": "text/plain",
                "size_bytes": 5,
                "original_name": "retry.txt",
            }

        def stream_authorized_locator(self, _locator, write_chunk):
            write_chunk(b"retry")
            return "text/plain"

    prepared = cache.prepare_file(
        "file_retry",
        {"task_id": "task_retry", "context_id": "ctx_retry"},
        client=RetryClient(),
    )

    assert prepared.local_path.read_bytes() == b"retry"
    assert getattr(cache, "_io_reservations", {}) == {}
    assert "task_retry" not in cache._cleaning_tasks


def test_task_attachment_cache_removes_partial_file_when_stream_exceeds_limit(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("QWENPAW_TASK_FILE_MAX_BYTES", "10")

    class OversizeStreamingClient:
        def authorize_file(self, file_id, _sandbox_context):
            return {
                "file_id": file_id,
                "storage_provider": "local",
                "object_key": f"runtime/{file_id}",
                "content_type": "application/octet-stream",
                "original_name": "oversize.bin",
            }

        def stream_authorized_locator(self, _locator, write_chunk):
            write_chunk(b"12345678")
            write_chunk(b"ABCDEFGH")
            return "application/octet-stream"

    cache = runtime_sandbox_oss_module.TaskAttachmentCache(root=tmp_path)

    with pytest.raises(RuntimeError, match="too large"):
        cache.prepare_file(
            "file_oversize",
            {"task_id": "task_oversize", "context_id": "ctx_oversize"},
            client=OversizeStreamingClient(),
        )

    assert not list(tmp_path.rglob("oversize.bin"))
    assert not list(tmp_path.rglob("*.part.*"))


def test_task_attachment_cache_removes_partial_file_when_stream_fails(
    tmp_path,
) -> None:
    class FailingStreamingClient:
        def authorize_file(self, file_id, _sandbox_context):
            return {
                "file_id": file_id,
                "storage_provider": "local",
                "object_key": f"runtime/{file_id}",
                "content_type": "text/plain",
                "original_name": "broken.txt",
            }

        def stream_authorized_locator(self, _locator, write_chunk):
            write_chunk(b"partial")
            raise OSError("connection lost")

    cache = runtime_sandbox_oss_module.TaskAttachmentCache(root=tmp_path)

    with pytest.raises(OSError, match="connection lost"):
        cache.prepare_file(
            "file_broken",
            {"task_id": "task_broken", "context_id": "ctx_broken"},
            client=FailingStreamingClient(),
        )

    assert not list(tmp_path.rglob("broken.txt"))
    assert not list(tmp_path.rglob("*.part.*"))


def test_task_marker_exists_before_stream_and_survives_stream_failure(
    tmp_path,
) -> None:
    cache = runtime_sandbox_oss_module.TaskAttachmentCache(root=tmp_path)
    marker_observed: list[tuple[bool, bool]] = []

    class FailingStreamingClient:
        def authorize_file(self, file_id, _sandbox_context):
            return {
                "file_id": file_id,
                "storage_provider": "local",
                "object_key": f"runtime/{file_id}",
                "content_type": "text/plain",
                "original_name": "sensitive.txt",
            }

        def stream_authorized_locator(self, _locator, write_chunk):
            marker_observed.append(
                (
                    cache._task_marker_path("task_marker").is_file(),
                    "task_marker" in cache._markers,
                ),
            )
            write_chunk(b"partial-sensitive-content")
            raise OSError("stream interrupted")

    with pytest.raises(OSError, match="stream interrupted"):
        cache.prepare_file(
            "file_marker",
            {"task_id": "task_marker", "context_id": "ctx_marker"},
            client=FailingStreamingClient(),
        )

    assert marker_observed == [(True, True)]
    assert cache._task_marker_path("task_marker").is_file()
    assert "task_marker" in cache._markers
    assert not list(tmp_path.rglob("sensitive.txt"))
    assert not list(tmp_path.rglob("*.part.*"))


def test_task_marker_failure_does_not_start_stream_or_create_files(
    tmp_path,
    monkeypatch,
) -> None:
    cache = runtime_sandbox_oss_module.TaskAttachmentCache(root=tmp_path)
    stream_called = False

    class StreamingClient:
        def authorize_file(self, file_id, _sandbox_context):
            return {
                "file_id": file_id,
                "storage_provider": "local",
                "object_key": f"runtime/{file_id}",
                "content_type": "text/plain",
                "size_bytes": 6,
                "original_name": "sensitive.txt",
            }

        def stream_authorized_locator(self, _locator, write_chunk):
            nonlocal stream_called
            stream_called = True
            write_chunk(b"secret")
            return "text/plain"

    def fail_marker_write(_marker) -> None:
        raise OSError("marker disk unavailable")

    monkeypatch.setattr(cache, "_write_task_marker", fail_marker_write)

    with pytest.raises(OSError, match="marker disk unavailable"):
        cache.prepare_file(
            "file_marker",
            {"task_id": "task_marker", "context_id": "ctx_marker"},
            client=StreamingClient(),
        )

    assert stream_called is False
    assert "task_marker" not in cache._markers
    assert not list(tmp_path.rglob("sensitive.txt"))
    assert not list(tmp_path.rglob("*.part.*"))


def test_concurrent_same_task_marker_writes_remain_valid(
    tmp_path,
) -> None:
    cache = runtime_sandbox_oss_module.TaskAttachmentCache(root=tmp_path)
    stream_barrier = threading.Barrier(2)

    class ConcurrentStreamingClient:
        def authorize_file(self, file_id, _sandbox_context):
            return {
                "file_id": file_id,
                "storage_provider": "local",
                "object_key": f"runtime/{file_id}",
                "content_type": "text/plain",
                "size_bytes": len(file_id),
                "original_name": f"{file_id}.txt",
            }

        def stream_authorized_locator(self, locator, write_chunk):
            stream_barrier.wait(timeout=2)
            write_chunk(str(locator["file_id"]).encode("utf-8"))
            return "text/plain"

    context = {"task_id": "task_marker", "context_id": "ctx_marker"}
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            cache.prepare_file,
            "file_a",
            context,
            client=ConcurrentStreamingClient(),
        )
        second = executor.submit(
            cache.prepare_file,
            "file_b",
            context,
            client=ConcurrentStreamingClient(),
        )
        first.result(timeout=2)
        second.result(timeout=2)

    marker_path = cache._task_marker_path("task_marker")
    marker_payload = json.loads(marker_path.read_text(encoding="utf-8"))
    assert marker_payload["task_id"] == "task_marker"
    assert marker_payload["sandbox_context_id"] == "ctx_marker"
    assert cache._markers["task_marker"].task_id == "task_marker"
    assert not list(tmp_path.rglob("*.part.*"))


def test_sandboxed_oss_client_streams_local_object_in_bounded_chunks(
    tmp_path,
    monkeypatch,
) -> None:
    object_root = tmp_path / "objects"
    object_path = object_root / "runtime" / "large.bin"
    object_path.parent.mkdir(parents=True)
    object_path.write_bytes(b"z" * (2 * 1024 * 1024 + 17))
    monkeypatch.setenv("QWENPAW_LOCAL_OBJECT_ROOT", str(object_root))
    chunk_sizes: list[int] = []
    total = 0

    def collect(chunk: bytes) -> None:
        nonlocal total
        chunk_sizes.append(len(chunk))
        total += len(chunk)

    content_type = runtime_sandbox_oss_module.SandboxedOssClient().stream_authorized_locator(
        {
            "storage_provider": "local",
            "object_key": "runtime/large.bin",
            "content_type": "application/octet-stream",
            "size_bytes": object_path.stat().st_size,
        },
        collect,
    )

    assert content_type == "application/octet-stream"
    assert total == object_path.stat().st_size
    assert len(chunk_sizes) >= 3
    assert max(chunk_sizes) <= 1024 * 1024


def test_sandboxed_oss_client_streams_oss_object_in_bounded_chunks(monkeypatch) -> None:
    payload = b"o" * (2 * 1024 * 1024 + 19)
    read_sizes: list[int] = []
    bucket_endpoints: list[str] = []

    class FakeObject:
        def __init__(self) -> None:
            self.offset = 0
            self.closed = False

        def read(self, size: int) -> bytes:
            read_sizes.append(size)
            chunk = payload[self.offset : self.offset + size]
            self.offset += len(chunk)
            return chunk

        def close(self) -> None:
            self.closed = True

    fake_object = FakeObject()

    class FakeBucket:
        def __init__(self, _auth, endpoint: str, _bucket_name: str) -> None:
            bucket_endpoints.append(endpoint)

        def get_object(self, object_key: str):
            assert object_key == "runtime/large.bin"
            return fake_object

    fake_oss2 = SimpleNamespace(
        Auth=lambda *_args: object(),
        Bucket=FakeBucket,
    )
    monkeypatch.setitem(sys.modules, "oss2", fake_oss2)
    monkeypatch.setenv("OSS_ENDPOINT", "oss.example")
    monkeypatch.setenv("OSS_BUCKET", "runtime-bucket")
    monkeypatch.setenv("OSS_ACCESS_KEY_ID", "test-key")
    monkeypatch.setenv("OSS_ACCESS_KEY_SECRET", "test-secret")
    chunks: list[bytes] = []

    content_type = runtime_sandbox_oss_module.SandboxedOssClient().stream_authorized_locator(
        {
            "storage_provider": "oss",
            "object_key": "runtime/large.bin",
            "content_type": "application/octet-stream",
            "size_bytes": len(payload),
        },
        chunks.append,
    )

    assert content_type == "application/octet-stream"
    assert b"".join(chunks) == payload
    assert fake_object.closed is True
    assert bucket_endpoints == ["https://oss.example"]
    assert read_sizes
    assert max(read_sizes) <= 1024 * 1024


def test_prepare_files_authorizes_manifest_once_and_preserves_order(tmp_path) -> None:
    class FakeBatchClient:
        def __init__(self):
            self.batch_calls: list[list[str]] = []
            self.read_calls: list[str] = []

        def authorize_files(self, file_ids: list[str], sandbox_context: dict):
            self.batch_calls.append(list(file_ids))
            return {
                "authorized": [
                    {
                        "file_id": file_id,
                        "storage_provider": "local",
                        "object_key": f"runtime/{file_id}",
                        "content_type": "text/plain",
                        "size_bytes": len(file_id),
                        "original_name": f"{file_id}.txt",
                    }
                    for file_id in file_ids
                ],
                "denied": [],
            }

        def authorize_file(self, *_args, **_kwargs):
            raise AssertionError("batch preparation must not authorize per file")

        def stream_authorized_locator(self, locator: dict, write_chunk):
            file_id = str(locator["file_id"])
            self.read_calls.append(file_id)
            write_chunk(file_id.encode("utf-8"))
            return "text/plain"

    client = FakeBatchClient()
    cache = runtime_sandbox_oss_module.TaskAttachmentCache(root=tmp_path)
    sandbox_context = {"task_id": "task_a", "context_id": "ctx_a"}

    prepared = cache.prepare_files(
        ["file_a", "file_b", "file_a"],
        sandbox_context,
        client=client,
    )

    assert [item.file_id for item in prepared] == ["file_a", "file_b"]
    assert client.batch_calls == [["file_a", "file_b"]]
    assert client.read_calls == ["file_a", "file_b"]


def test_prepare_files_batch_authorizes_only_cache_misses(tmp_path) -> None:
    class FakeBatchClient:
        def __init__(self):
            self.batch_calls: list[list[str]] = []
            self.read_calls: list[str] = []

        def authorize_files(self, file_ids: list[str], sandbox_context: dict):
            self.batch_calls.append(list(file_ids))
            return {
                "authorized": [
                    {
                        "file_id": file_id,
                        "storage_provider": "local",
                        "object_key": f"runtime/{file_id}",
                        "content_type": "text/plain",
                        "size_bytes": len(file_id),
                        "original_name": f"{file_id}.txt",
                    }
                    for file_id in file_ids
                ],
                "denied": [],
            }

        def stream_authorized_locator(self, locator: dict, write_chunk):
            file_id = str(locator["file_id"])
            self.read_calls.append(file_id)
            write_chunk(file_id.encode("utf-8"))
            return "text/plain"

    client = FakeBatchClient()
    cache = runtime_sandbox_oss_module.TaskAttachmentCache(root=tmp_path)
    sandbox_context = {"task_id": "task_a", "context_id": "ctx_a"}

    first = cache.prepare_files(["file_a"], sandbox_context, client=client)
    second = cache.prepare_files(
        ["file_a", "file_b"],
        sandbox_context,
        client=client,
    )

    assert [item.file_id for item in first] == ["file_a"]
    assert [item.file_id for item in second] == ["file_a", "file_b"]
    assert client.batch_calls == [["file_a"], ["file_b"]]
    assert client.read_calls == ["file_a", "file_b"]


def test_prepare_files_denial_raises_typed_error_before_download(tmp_path) -> None:
    class DenyingBatchClient:
        def __init__(self):
            self.read_called = False

        def authorize_files(self, file_ids: list[str], sandbox_context: dict):
            return {
                "authorized": [
                    {
                        "file_id": "file_a",
                        "storage_provider": "local",
                        "object_key": "runtime/file_a",
                        "content_type": "text/plain",
                        "size_bytes": 1,
                        "original_name": "a.txt",
                    },
                ],
                "denied": [
                    {
                        "file_id": "file_b",
                        "reason_code": "FILE_ACCESS_DENIED",
                    },
                ],
            }

        def stream_authorized_locator(self, _locator: dict, _write_chunk):
            self.read_called = True
            raise AssertionError("denied manifest must fail before download")

    client = DenyingBatchClient()
    cache = runtime_sandbox_oss_module.TaskAttachmentCache(root=tmp_path)

    with pytest.raises(
        runtime_sandbox_oss_module.RuntimeAttachmentPreparationError,
    ) as exc_info:
        cache.prepare_files(
            ["file_a", "file_b"],
            {"task_id": "task_a", "context_id": "ctx_a"},
            client=client,
        )

    assert exc_info.value.file_id == "file_b"
    assert exc_info.value.reason_code == "FILE_ACCESS_DENIED"
    assert client.read_called is False


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

        def stream_authorized_locator(self, locator: dict, write_chunk):
            self.read_calls.append(dict(locator))
            write_chunk(b"cached\n")
            return str(locator["content_type"])

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

        def stream_authorized_locator(self, locator: dict, write_chunk):
            self.read_calls.append(dict(locator))
            write_chunk(str(locator["object_key"]).encode("utf-8"))
            return str(locator["content_type"])

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

        def stream_authorized_locator(self, _locator: dict, _write_chunk):
            self.read_called = True
            raise AssertionError("oversized locator must fail before download")

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

        def stream_authorized_locator(self, locator: dict, write_chunk):
            with self._lock:
                self.read_calls.append(dict(locator))
            self.first_read_started.set()
            assert self.allow_reads_to_finish.wait(timeout=2)
            write_chunk(b"cached\n")
            return str(locator["content_type"])

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


def test_task_attachment_cache_concurrent_downloads_share_total_byte_quota(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("QWENPAW_TASK_FILE_MAX_COUNT", "20")
    monkeypatch.setenv("QWENPAW_TASK_FILE_MAX_TOTAL_BYTES", "10")
    first_stream_started = threading.Event()
    release_first_stream = threading.Event()
    stream_calls: list[str] = []
    stream_calls_lock = threading.Lock()

    class QuotaClient:
        def authorize_file(self, file_id, _sandbox_context):
            return {
                "file_id": file_id,
                "storage_provider": "local",
                "object_key": f"runtime/{file_id}",
                "content_type": "text/plain",
                "size_bytes": 6,
                "original_name": f"{file_id}.txt",
            }

        def stream_authorized_locator(self, locator, write_chunk):
            file_id = str(locator["file_id"])
            with stream_calls_lock:
                stream_calls.append(file_id)
            if file_id == "file_a":
                first_stream_started.set()
                assert release_first_stream.wait(timeout=2)
            write_chunk(b"123456")
            return "text/plain"

    cache = runtime_sandbox_oss_module.TaskAttachmentCache(root=tmp_path)
    context = {"task_id": "task_quota", "context_id": "ctx_quota"}
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            cache.prepare_file,
            "file_a",
            context,
            client=QuotaClient(),
        )
        assert first_stream_started.wait(timeout=2)
        second = executor.submit(
            cache.prepare_file,
            "file_b",
            context,
            client=QuotaClient(),
        )
        with pytest.raises(RuntimeError, match="quota"):
            second.result(timeout=2)
        release_first_stream.set()
        prepared = first.result(timeout=2)

    assert prepared.local_path.read_bytes() == b"123456"
    assert stream_calls == ["file_a"]
    assert not list(tmp_path.rglob("file_b.txt"))
    assert not list(tmp_path.rglob("*.part.*"))


def test_task_attachment_cache_hit_does_not_consume_file_quota_twice(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("QWENPAW_TASK_FILE_MAX_COUNT", "1")
    monkeypatch.setenv("QWENPAW_TASK_FILE_MAX_TOTAL_BYTES", "20")
    stream_calls: list[str] = []

    class QuotaClient:
        def authorize_file(self, file_id, _sandbox_context):
            return {
                "file_id": file_id,
                "storage_provider": "local",
                "object_key": f"runtime/{file_id}",
                "content_type": "text/plain",
                "size_bytes": 4,
                "original_name": f"{file_id}.txt",
            }

        def stream_authorized_locator(self, locator, write_chunk):
            stream_calls.append(str(locator["file_id"]))
            write_chunk(b"data")
            return "text/plain"

    cache = runtime_sandbox_oss_module.TaskAttachmentCache(root=tmp_path)
    context = {"task_id": "task_quota", "context_id": "ctx_quota"}
    first = cache.prepare_file("file_a", context, client=QuotaClient())
    second = cache.prepare_file("file_a", context, client=QuotaClient())

    with pytest.raises(RuntimeError, match="quota"):
        cache.prepare_file("file_b", context, client=QuotaClient())

    assert first.local_path == second.local_path
    assert stream_calls == ["file_a"]
    cache.cleanup_task("task_quota")
    assert cache._quota_committed == {}
    assert cache._quota_reservations == {}


def test_task_attachment_cache_unknown_size_enforces_remaining_task_quota(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("QWENPAW_TASK_FILE_MAX_COUNT", "20")
    monkeypatch.setenv("QWENPAW_TASK_FILE_MAX_TOTAL_BYTES", "5")

    class UnknownSizeClient:
        def authorize_file(self, file_id, _sandbox_context):
            return {
                "file_id": file_id,
                "storage_provider": "local",
                "object_key": f"runtime/{file_id}",
                "content_type": "application/octet-stream",
                "original_name": "unknown.bin",
            }

        def stream_authorized_locator(self, _locator, write_chunk):
            write_chunk(b"123")
            write_chunk(b"456")
            return "application/octet-stream"

    cache = runtime_sandbox_oss_module.TaskAttachmentCache(root=tmp_path)

    with pytest.raises(RuntimeError, match="quota"):
        cache.prepare_file(
            "file_unknown",
            {"task_id": "task_quota", "context_id": "ctx_quota"},
            client=UnknownSizeClient(),
        )

    assert not list(tmp_path.rglob("unknown.bin"))
    assert not list(tmp_path.rglob("*.part.*"))
    assert cache._quota_committed == {}
    assert cache._quota_reservations == {}


def test_prepare_files_concurrent_duplicate_batch_authorizes_once(tmp_path) -> None:
    class BlockingBatchClient:
        def __init__(self):
            self.authorize_calls = 0
            self.read_calls = 0
            self._lock = threading.Lock()
            self.first_authorization_started = threading.Event()
            self.allow_authorization_to_finish = threading.Event()

        def authorize_files(self, file_ids: list[str], sandbox_context: dict):
            with self._lock:
                self.authorize_calls += 1
            self.first_authorization_started.set()
            assert self.allow_authorization_to_finish.wait(timeout=2)
            return {
                "authorized": [
                    {
                        "file_id": file_ids[0],
                        "storage_provider": "local",
                        "object_key": f"runtime/{file_ids[0]}",
                        "content_type": "text/plain",
                        "size_bytes": 7,
                        "original_name": "customer.txt",
                    },
                ],
                "denied": [],
            }

        def stream_authorized_locator(self, locator: dict, write_chunk):
            with self._lock:
                self.read_calls += 1
            write_chunk(b"cached\n")
            return str(locator["content_type"])

    client = BlockingBatchClient()
    cache = runtime_sandbox_oss_module.TaskAttachmentCache(root=tmp_path)
    sandbox_context = {"task_id": "task_001", "context_id": "ctx_001"}
    start = threading.Barrier(3)

    def prepare():
        start.wait(timeout=2)
        return cache.prepare_files(
            ["file_001"],
            sandbox_context,
            client=client,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(prepare)
        second_future = executor.submit(prepare)
        start.wait(timeout=2)
        assert client.first_authorization_started.wait(timeout=2)
        time.sleep(0.1)
        client.allow_authorization_to_finish.set()
        first = first_future.result(timeout=2)
        second = second_future.result(timeout=2)

    assert first[0].local_path == second[0].local_path
    assert client.authorize_calls == 1
    assert client.read_calls == 1


def test_cleanup_waits_for_active_batch_authorization(tmp_path) -> None:
    class BlockingBatchClient:
        def __init__(self):
            self.authorization_started = threading.Event()
            self.allow_authorization_to_finish = threading.Event()

        def authorize_files(self, file_ids: list[str], sandbox_context: dict):
            self.authorization_started.set()
            assert self.allow_authorization_to_finish.wait(timeout=2)
            return {
                "authorized": [
                    {
                        "file_id": file_ids[0],
                        "storage_provider": "local",
                        "object_key": f"runtime/{file_ids[0]}",
                        "content_type": "text/plain",
                        "size_bytes": 7,
                        "original_name": "customer.txt",
                    },
                ],
                "denied": [],
            }

        def stream_authorized_locator(self, locator: dict, write_chunk):
            write_chunk(b"cached\n")
            return str(locator["content_type"])

    client = BlockingBatchClient()
    cache = runtime_sandbox_oss_module.TaskAttachmentCache(root=tmp_path)
    sandbox_context = {"task_id": "task_001", "context_id": "ctx_001"}

    with ThreadPoolExecutor(max_workers=2) as executor:
        prepare_future = executor.submit(
            cache.prepare_files,
            ["file_001"],
            sandbox_context,
            client=client,
        )
        assert client.authorization_started.wait(timeout=2)
        cleanup_future = executor.submit(cache.cleanup_task, "task_001")
        with pytest.raises(FutureTimeoutError):
            cleanup_future.result(timeout=0.05)

        client.allow_authorization_to_finish.set()
        with pytest.raises(
            runtime_sandbox_oss_module.RuntimeAttachmentPreparationError,
        ):
            prepare_future.result(timeout=2)
        cleanup_future.result(timeout=2)

    assert not any(key[0] == "task_001" for key in cache._batch_locks)
    assert not any(key[0] == "task_001" for key in cache._download_locks)


def test_prepare_files_does_not_deadlock_when_marker_expires_inside_batch(
    tmp_path,
) -> None:
    class SlowBatchClient:
        def authorize_files(self, file_ids: list[str], sandbox_context: dict):
            time.sleep(0.3)
            return {
                "authorized": [
                    {
                        "file_id": file_ids[0],
                        "storage_provider": "local",
                        "object_key": f"runtime/{file_ids[0]}",
                        "content_type": "text/plain",
                        "size_bytes": 7,
                        "original_name": "customer.txt",
                    },
                ],
                "denied": [],
            }

        def stream_authorized_locator(self, locator: dict, write_chunk):
            write_chunk(b"cached\n")
            return str(locator["content_type"])

    cache = runtime_sandbox_oss_module.TaskAttachmentCache(root=tmp_path)
    now = time.time()
    cache._markers["task_001"] = runtime_sandbox_oss_module.TaskMarker(
        task_id="task_001",
        sandbox_context_id="ctx_001",
        created_at_epoch=now,
        expires_at_epoch=now + 0.2,
    )
    result: dict[str, object] = {}

    def prepare() -> None:
        try:
            result["prepared"] = cache.prepare_files(
                ["file_001"],
                {"task_id": "task_001", "context_id": "ctx_001"},
                client=SlowBatchClient(),
            )
        except BaseException as exc:  # pragma: no cover - diagnostic capture
            result["error"] = exc

    worker = threading.Thread(target=prepare, daemon=True)
    worker.start()
    worker.join(timeout=1)

    assert worker.is_alive() is False, "prepare_files deadlocked during TTL cleanup"
    assert "error" not in result
    assert result["prepared"][0].file_id == "file_001"


def test_cross_task_ttl_sweep_skips_reserved_tasks_without_deadlock(
    tmp_path,
) -> None:
    cache = runtime_sandbox_oss_module.TaskAttachmentCache(root=tmp_path)
    now = time.time()
    for task_id, context_id in (
        ("task_a", "ctx_a"),
        ("task_b", "ctx_b"),
    ):
        cache._markers[task_id] = runtime_sandbox_oss_module.TaskMarker(
            task_id=task_id,
            sandbox_context_id=context_id,
            created_at_epoch=now - 10,
            expires_at_epoch=now - 1,
        )

    class StreamingClient:
        def authorize_file(self, file_id, _sandbox_context):
            return {
                "file_id": file_id,
                "storage_provider": "local",
                "object_key": f"runtime/{file_id}",
                "content_type": "text/plain",
                "size_bytes": 4,
                "original_name": f"{file_id}.txt",
            }

        def stream_authorized_locator(self, _locator, write_chunk):
            write_chunk(b"data")
            return "text/plain"

    barrier = threading.Barrier(2)
    original_sweep = cache.sweep_expired

    def synchronized_sweep(*args, **kwargs):
        barrier.wait(timeout=1)
        return original_sweep(*args, **kwargs)

    cache.sweep_expired = synchronized_sweep
    errors: list[BaseException] = []

    def prepare(task_id: str, context_id: str, file_id: str) -> None:
        try:
            cache.prepare_file(
                file_id,
                {"task_id": task_id, "context_id": context_id},
                client=StreamingClient(),
            )
        except BaseException as exc:  # pragma: no cover - diagnostic capture
            errors.append(exc)

    first = threading.Thread(
        target=prepare,
        args=("task_a", "ctx_a", "file_a"),
        daemon=True,
    )
    second = threading.Thread(
        target=prepare,
        args=("task_b", "ctx_b", "file_b"),
        daemon=True,
    )
    first.start()
    second.start()
    first.join(timeout=2)
    second.join(timeout=2)

    assert first.is_alive() is False
    assert second.is_alive() is False
    assert errors == []

    cache.cleanup_task("task_a")
    cache.cleanup_task("task_b")
    assert cache._cleaning_tasks == set()
    assert cache._io_reservations == {}
    assert cache._batch_lock_users == {}
    assert cache._batch_locks == {}
    assert cache._download_locks == {}
    assert cache._cleanup_users == {}
    assert cache._cleanup_locks == {}


def test_concurrent_cleanup_keeps_task_blocked_until_last_cleanup_finishes(
    tmp_path,
    monkeypatch,
) -> None:
    cache = runtime_sandbox_oss_module.TaskAttachmentCache(root=tmp_path)
    first_cleanup_entered = threading.Event()
    second_cleanup_entered = threading.Event()
    allow_first_cleanup = threading.Event()
    allow_second_cleanup = threading.Event()
    cleanup_calls = 0
    cleanup_calls_lock = threading.Lock()

    def blocking_rmtree(*_args, **_kwargs):
        nonlocal cleanup_calls
        with cleanup_calls_lock:
            cleanup_calls += 1
            call_number = cleanup_calls
        if call_number == 1:
            first_cleanup_entered.set()
            assert allow_first_cleanup.wait(timeout=2)
        elif call_number == 2:
            second_cleanup_entered.set()
            assert allow_second_cleanup.wait(timeout=2)

    monkeypatch.setattr(
        runtime_sandbox_oss_module.shutil,
        "rmtree",
        blocking_rmtree,
    )

    class ProbeBatchClient:
        def __init__(self):
            self.authorize_calls = 0

        def authorize_files(self, *_args, **_kwargs):
            self.authorize_calls += 1
            raise AssertionError("new batch must be rejected during cleanup")

    probe_client = ProbeBatchClient()
    first = threading.Thread(target=cache.cleanup_task, args=("task_001",), daemon=True)
    second = threading.Thread(target=cache.cleanup_task, args=("task_001",), daemon=True)
    first.start()
    assert first_cleanup_entered.wait(timeout=2)
    second.start()

    try:
        deadline = time.time() + 1
        while (
            getattr(cache, "_cleanup_users", {}).get("task_001", 0) < 2
            and time.time() < deadline
        ):
            time.sleep(0.01)
        assert cache._cleanup_users["task_001"] == 2
        assert "task_001" in cache._cleaning_tasks
        with pytest.raises(
            runtime_sandbox_oss_module.RuntimeAttachmentPreparationError,
        ):
            cache.prepare_files(
                ["file_001"],
                {"task_id": "task_001", "context_id": "ctx_001"},
                client=probe_client,
            )

        allow_first_cleanup.set()
        assert second_cleanup_entered.wait(timeout=2)
        first.join(timeout=2)
        assert first.is_alive() is False
        assert "task_001" in cache._cleaning_tasks
        assert cache._cleanup_users["task_001"] == 1
        with pytest.raises(
            runtime_sandbox_oss_module.RuntimeAttachmentPreparationError,
        ):
            cache.prepare_files(
                ["file_001"],
                {"task_id": "task_001", "context_id": "ctx_001"},
                client=probe_client,
            )
    finally:
        allow_first_cleanup.set()
        allow_second_cleanup.set()
        first.join(timeout=2)
        second.join(timeout=2)

    assert first.is_alive() is False
    assert second.is_alive() is False
    assert probe_client.authorize_calls == 0
    assert "task_001" not in cache._cleaning_tasks
    assert "task_001" not in cache._cleanup_users
    assert "task_001" not in cache._cleanup_locks
    assert not any(key[0] == "task_001" for key in cache._batch_locks)


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

        def stream_authorized_locator(self, _locator: dict, _write_chunk):
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

        def stream_authorized_locator(self, _locator: dict, _write_chunk):
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

        def stream_authorized_locator(self, locator: dict, write_chunk):
            self.read_started.set()
            assert self.release_read.wait(timeout=2)
            write_chunk(b"cached\n")
            return str(locator["content_type"])

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

        def stream_authorized_locator(self, locator: dict, write_chunk):
            write_chunk(str(locator["object_key"]).encode("utf-8"))
            return str(locator["content_type"])

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

        def stream_authorized_locator(self, locator: dict, write_chunk):
            write_chunk(b"cached\n")
            return str(locator["content_type"])

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


def test_task_attachment_cache_background_sweeper_cleans_idle_expired_task(
    tmp_path,
) -> None:
    cache = runtime_sandbox_oss_module.TaskAttachmentCache(root=tmp_path)
    assert cache._sweeper_thread is None
    now = time.time()
    marker = runtime_sandbox_oss_module.TaskMarker(
        task_id="task_idle",
        sandbox_context_id="ctx_idle",
        created_at_epoch=now - 10,
        expires_at_epoch=now - 1,
    )
    cache._ensure_private_task_root("task_idle")
    cache._write_task_marker(marker)
    cache._markers["task_idle"] = marker
    task_root = cache._task_root("task_idle")

    cache.start_sweeper(interval_seconds=0.01)
    try:
        deadline = time.time() + 2
        while task_root.exists() and time.time() < deadline:
            time.sleep(0.01)
        assert task_root.exists() is False
    finally:
        cache.stop_sweeper(timeout=2)

    assert cache._sweeper_thread is None


def test_task_attachment_cache_background_sweeper_skips_active_task(
    tmp_path,
) -> None:
    cache = runtime_sandbox_oss_module.TaskAttachmentCache(root=tmp_path)
    now = time.time()
    marker = runtime_sandbox_oss_module.TaskMarker(
        task_id="task_active",
        sandbox_context_id="ctx_active",
        created_at_epoch=now - 10,
        expires_at_epoch=now - 1,
    )
    cache._ensure_private_task_root("task_active")
    cache._write_task_marker(marker)
    cache._markers["task_active"] = marker
    task_root = cache._task_root("task_active")
    reservation = cache.reserve_task_io("task_active")

    cache.start_sweeper(interval_seconds=0.01)
    try:
        time.sleep(0.08)
        assert task_root.is_dir()
        assert cache._io_reservations == {"task_active": 1}
        reservation.release()
        deadline = time.time() + 2
        while task_root.exists() and time.time() < deadline:
            time.sleep(0.01)
        assert task_root.exists() is False
    finally:
        reservation.release()
        cache.stop_sweeper(timeout=2)

    assert cache._io_reservations == {}
    assert cache._sweeper_thread is None


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

        def stream_authorized_locator(self, locator: dict, write_chunk):
            write_chunk(b"cached\n")
            return str(locator["content_type"])

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

        def stream_authorized_locator(self, locator: dict, write_chunk):
            write_chunk(b"cached\n")
            return str(locator["content_type"])

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

        def stream_authorized_locator(self, locator: dict, write_chunk):
            write_chunk(b"cached\n")
            return str(locator["content_type"])

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
        client.stream_authorized_locator(
            {
                "storage_provider": "local",
                "object_key": "runtime/file_001",
                "content_type": "text/plain",
                "size_bytes": 0,
            },
            lambda _chunk: None,
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

        def stream_authorized_locator(self, locator: dict, write_chunk):
            write_chunk(b"secret")
            return str(locator["content_type"])

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


def test_safe_filename_limits_utf8_bytes_and_preserves_safe_extension() -> None:
    original_name = "银行客户材料" * 60 + ".markdown"

    safe_name = runtime_sandbox_oss_module._safe_original_name(original_name)

    assert len(safe_name.encode("utf-8")) <= 255
    assert safe_name.endswith(".markdown")
    assert safe_name != original_name


def test_safe_filename_leaves_room_for_atomic_temp_suffix(tmp_path) -> None:
    original_name = "银行客户材料" * 60 + ".markdown"
    safe_name = runtime_sandbox_oss_module._safe_original_name(original_name)
    target = tmp_path / safe_name

    def stream_to(write_chunk):
        write_chunk(b"content")
        return "text/markdown"

    size_bytes, content_type = runtime_sandbox_oss_module._write_stream_atomic(
        target,
        stream_to,
    )

    assert len(f"{safe_name}.part.{'0' * 32}".encode("utf-8")) <= 255
    assert target.read_bytes() == b"content"
    assert size_bytes == 7
    assert content_type == "text/markdown"


def test_prepared_image_becomes_image_content(tmp_path) -> None:
    local_path = tmp_path / "photo.png"
    local_path.write_bytes(b"png")
    prepared = runtime_sandbox_oss_module.PreparedSandboxFile(
        file_id="file_001",
        local_path=local_path,
        content_type="image/png",
        size_bytes=3,
        original_name="photo.png",
        expires_at="2999-01-01T00:00:00+08:00",
    )

    content_part = runtime_sandbox_oss_module.content_part_for_prepared_file(prepared)

    assert content_part["type"] == "image"
    assert content_part["source"]["type"] == "url"
    assert content_part["source"]["url"] == local_path.resolve().as_uri()
    assert content_part["_runtime_sandbox_attachment"] is True
    assert content_part["_runtime_attachment_file_id"] == "file_001"


def test_prepared_markdown_becomes_file_content(tmp_path) -> None:
    local_path = tmp_path / "cline-rules.md"
    local_path.write_text("# rules", encoding="utf-8")
    prepared = runtime_sandbox_oss_module.PreparedSandboxFile(
        file_id="file_001",
        local_path=local_path,
        content_type="text/markdown",
        size_bytes=7,
        original_name="cline-rules.md",
        expires_at="2999-01-01T00:00:00+08:00",
    )

    content_part = runtime_sandbox_oss_module.content_part_for_prepared_file(prepared)

    assert content_part["type"] == "file"
    assert content_part["filename"] == "cline-rules.md"
    assert content_part["source"]["type"] == "url"
    assert content_part["source"]["url"] == local_path.resolve().as_uri()
    assert content_part["_runtime_sandbox_attachment"] is True
    assert content_part["_runtime_attachment_file_id"] == "file_001"


@pytest.mark.parametrize(
    ("content_type", "expected_type", "filename"),
    [
        ("video/mp4", "video", "clip.mp4"),
        ("audio/wav", "audio", "voice.wav"),
    ],
)
def test_prepared_media_becomes_matching_media_content(
    tmp_path,
    content_type,
    expected_type,
    filename,
) -> None:
    local_path = tmp_path / filename
    local_path.write_bytes(b"media")
    prepared = runtime_sandbox_oss_module.PreparedSandboxFile(
        file_id="file_001",
        local_path=local_path,
        content_type=content_type,
        size_bytes=5,
        original_name=filename,
        expires_at="2999-01-01T00:00:00+08:00",
    )

    content_part = runtime_sandbox_oss_module.content_part_for_prepared_file(prepared)

    assert content_part["type"] == expected_type
    assert content_part["source"]["type"] == "url"
    assert content_part["source"]["url"] == local_path.resolve().as_uri()


def test_runtime_attachment_prompt_does_not_show_local_paths() -> None:
    react_agent = importlib.import_module("qwenpaw.agents.react_agent")
    context = {
        "sandbox_context": {"task_id": "task_001"},
        "attachments_manifest": [
            {
                "file_id": "file_001",
                "original_name": "cline-rules.md",
                "content_type": "text/markdown",
                "size_bytes": 8,
                "access_mode": "sandbox_oss",
            }
        ],
    }

    prompt = "\n".join(react_agent._build_runtime_attachments_context(context))

    assert "runtime_attachment_read" in prompt
    assert "file_001" in prompt
    assert "file://" not in prompt
    assert "object_key" not in prompt
    assert "bucket" not in prompt


@pytest.mark.asyncio
async def test_append_runtime_attachment_content_parts_adds_current_task_files(
    monkeypatch,
    tmp_path,
) -> None:
    react_agent = importlib.import_module("qwenpaw.agents.react_agent")
    local_path = tmp_path / "photo.png"
    local_path.write_bytes(b"png")
    prepared = runtime_sandbox_oss_module.PreparedSandboxFile(
        file_id="file_001",
        local_path=local_path,
        content_type="image/png",
        size_bytes=3,
        original_name="photo.png",
        expires_at="2999-01-01T00:00:00+08:00",
    )
    captured: dict[str, object] = {}

    class FakeTaskAttachmentCache(runtime_sandbox_oss_module.TaskAttachmentCache):
        def __init__(self):
            super().__init__(root=tmp_path / "cache")

        def prepare_files(self, file_ids: list[str], sandbox_context: dict):
            captured["file_ids"] = list(file_ids)
            captured["sandbox_context"] = dict(sandbox_context)
            return [prepared]

    monkeypatch.setattr(
        react_agent.runtime_sandbox_oss,
        "_DEFAULT_TASK_ATTACHMENT_CACHE",
        FakeTaskAttachmentCache(),
    )
    message = Msg("user", "识别一下图片", "user")
    request_context = {
        "sandbox_context": {"task_id": "task_001", "context_id": "ctx_001"},
        "attachments_manifest": [
            {
                "file_id": "file_001",
                "original_name": "photo.png",
                "content_type": "image/png",
                "access_mode": "sandbox_oss",
                "source": "current_task",
            },
            {
                "file_id": "file_002",
                "original_name": "history.png",
                "content_type": "image/png",
                "access_mode": "sandbox_oss",
                "source": "discovered",
            },
        ],
    }

    updated = await react_agent._append_runtime_attachment_content_parts(
        message,
        request_context,
    )

    assert updated is message
    assert captured["file_ids"] == ["file_001"]
    assert captured["sandbox_context"] == request_context["sandbox_context"]
    assert isinstance(message.content, list)
    assert message.content[0] == {"type": "text", "text": "识别一下图片"}
    assert message.content[1]["type"] == "image"
    assert message.content[1]["source"]["url"] == local_path.resolve().as_uri()


@pytest.mark.asyncio
async def test_current_task_attachment_denial_is_not_silently_ignored(
    monkeypatch,
) -> None:
    react_agent = importlib.import_module("qwenpaw.agents.react_agent")

    class DenyingTaskAttachmentCache:
        def prepare_files(self, file_ids: list[str], sandbox_context: dict):
            raise runtime_sandbox_oss_module.RuntimeAttachmentPreparationError(
                file_ids[0],
                "FILE_ACCESS_DENIED",
            )

    monkeypatch.setattr(
        react_agent.runtime_sandbox_oss,
        "_DEFAULT_TASK_ATTACHMENT_CACHE",
        DenyingTaskAttachmentCache(),
    )
    message = Msg("user", "识别一下图片", "user")
    request_context = {
        "sandbox_context": {"task_id": "task_001", "context_id": "ctx_001"},
        "attachments_manifest": [
            {
                "file_id": "file_denied",
                "original_name": "denied.png",
                "content_type": "image/png",
                "access_mode": "sandbox_oss",
                "source": "current_task",
            },
        ],
    }

    with pytest.raises(
        runtime_sandbox_oss_module.RuntimeAttachmentPreparationError,
    ):
        await react_agent._append_runtime_attachment_content_parts(
            message,
            request_context,
        )

    assert message.content == "识别一下图片"


@pytest.mark.asyncio
async def test_current_task_attachment_requires_sandbox_context() -> None:
    react_agent = importlib.import_module("qwenpaw.agents.react_agent")
    message = Msg("user", "识别一下图片", "user")
    request_context = {
        "attachments_manifest": [
            {
                "file_id": "file_current",
                "source": "current_task",
            },
        ],
    }

    with pytest.raises(
        runtime_sandbox_oss_module.RuntimeAttachmentPreparationError,
    ) as exc_info:
        await react_agent._append_runtime_attachment_content_parts(
            message,
            request_context,
        )

    assert exc_info.value.file_id == "file_current"
    assert exc_info.value.reason_code == "SANDBOX_CONTEXT_INVALID"
    assert message.content == "识别一下图片"


def test_current_task_attachment_selection_uses_source_not_access_metadata() -> None:
    react_agent = importlib.import_module("qwenpaw.agents.react_agent")

    file_ids = react_agent._runtime_current_task_attachment_ids(
        {
            "attachments_manifest": [
                {"file_id": "file_current", "source": "current_task"},
                {
                    "file_id": "file_discovered",
                    "source": "discovered",
                    "access_mode": "sandbox_oss",
                },
            ],
        },
    )

    assert file_ids == ["file_current"]


@pytest.mark.asyncio
async def test_append_runtime_attachment_content_parts_ignores_missing_source(
    monkeypatch,
    tmp_path,
) -> None:
    react_agent = importlib.import_module("qwenpaw.agents.react_agent")
    local_path = tmp_path / "legacy.png"
    local_path.write_bytes(b"png")
    prepared = runtime_sandbox_oss_module.PreparedSandboxFile(
        file_id="file_legacy",
        local_path=local_path,
        content_type="image/png",
        size_bytes=3,
        original_name="legacy.png",
        expires_at="2999-01-01T00:00:00+08:00",
    )
    captured: dict[str, object] = {}

    class FakeTaskAttachmentCache:
        def prepare_file(self, file_id: str, sandbox_context: dict):
            captured["file_id"] = file_id
            return prepared

    monkeypatch.setattr(
        react_agent.runtime_sandbox_oss,
        "_DEFAULT_TASK_ATTACHMENT_CACHE",
        FakeTaskAttachmentCache(),
    )
    message = Msg("user", "识别一下图片", "user")
    request_context = {
        "sandbox_context": {"task_id": "task_001", "context_id": "ctx_001"},
        "attachments_manifest": [
            {
                "file_id": "file_legacy",
                "original_name": "legacy.png",
                "content_type": "image/png",
                "access_mode": "sandbox_oss",
            },
        ],
    }

    updated = await react_agent._append_runtime_attachment_content_parts(
        message,
        request_context,
    )

    assert updated is message
    assert captured == {}
    assert message.content == "识别一下图片"


@pytest.mark.asyncio
async def test_runtime_attachment_preload_does_not_block_event_loop(
    monkeypatch,
    tmp_path,
) -> None:
    react_agent = importlib.import_module("qwenpaw.agents.react_agent")
    local_path = tmp_path / "photo.png"
    local_path.write_bytes(b"png")
    prepared = runtime_sandbox_oss_module.PreparedSandboxFile(
        file_id="file_001",
        local_path=local_path,
        content_type="image/png",
        size_bytes=3,
        original_name="photo.png",
        expires_at="2999-01-01T00:00:00+08:00",
    )
    preparation_started = threading.Event()
    release_preparation = threading.Event()
    observed: dict[str, bool] = {}

    class BlockingTaskAttachmentCache(
        runtime_sandbox_oss_module.TaskAttachmentCache,
    ):
        def __init__(self):
            super().__init__(root=tmp_path / "cache")

        def prepare_files(self, file_ids: list[str], sandbox_context: dict):
            preparation_started.set()
            observed["released_by_event_loop"] = release_preparation.wait(
                timeout=0.5,
            )
            return [prepared]

    monkeypatch.setattr(
        react_agent.runtime_sandbox_oss,
        "_DEFAULT_TASK_ATTACHMENT_CACHE",
        BlockingTaskAttachmentCache(),
    )
    message = Msg("user", "识别一下图片", "user")
    request_context = {
        "sandbox_context": {"task_id": "task_001", "context_id": "ctx_001"},
        "attachments_manifest": [
            {"file_id": "file_001", "source": "current_task"},
        ],
    }

    async def release_from_event_loop() -> None:
        while not preparation_started.is_set():
            await asyncio.sleep(0)
        release_preparation.set()

    release_task = asyncio.create_task(release_from_event_loop())
    updated = await react_agent._append_runtime_attachment_content_parts(
        message,
        request_context,
    )
    await release_task

    assert updated is message
    assert observed["released_by_event_loop"] is True


@pytest.mark.asyncio
async def test_runtime_attachment_preload_reserves_before_thread_submission(
    tmp_path,
    monkeypatch,
) -> None:
    react_agent = importlib.import_module("qwenpaw.agents.react_agent")
    cache = runtime_sandbox_oss_module.TaskAttachmentCache(root=tmp_path)
    local_path = tmp_path / "prepared.txt"
    local_path.write_text("prepared", encoding="utf-8")
    prepared = runtime_sandbox_oss_module.PreparedSandboxFile(
        file_id="file_reserved",
        local_path=local_path,
        content_type="text/plain",
        size_bytes=8,
        original_name="prepared.txt",
        expires_at="",
    )
    observed: list[int] = []

    def fake_prepare_files(*_args, **_kwargs):
        return [prepared]

    async def observe_thread_submission(function, *args, **kwargs):
        observed.append(cache._io_reservations.get("task_reserved", 0))
        return function(*args, **kwargs)

    cache.prepare_files = fake_prepare_files
    monkeypatch.setattr(
        runtime_sandbox_oss_module,
        "_DEFAULT_TASK_ATTACHMENT_CACHE",
        cache,
    )
    monkeypatch.setattr(
        runtime_sandbox_oss_module,
        "_run_in_thread",
        observe_thread_submission,
        raising=False,
    )

    updated = await react_agent._append_runtime_attachment_content_parts(
        Msg("user", "read", "user"),
        {
            "attachments_manifest": [
                {"file_id": "file_reserved", "source": "current_task"},
            ],
            "sandbox_context": {
                "task_id": "task_reserved",
                "context_id": "ctx_reserved",
            },
        },
    )

    assert isinstance(updated, Msg)
    assert observed == [1]
    assert cache._io_reservations == {}


@pytest.mark.asyncio
async def test_runtime_attachment_preload_preserves_cancelled_error(
    monkeypatch,
    tmp_path,
) -> None:
    react_agent = importlib.import_module("qwenpaw.agents.react_agent")

    class CancelledTaskAttachmentCache(
        runtime_sandbox_oss_module.TaskAttachmentCache,
    ):
        def __init__(self):
            super().__init__(root=tmp_path / "cache")

        def prepare_files(self, *_args, **_kwargs):
            raise asyncio.CancelledError()

    monkeypatch.setattr(
        react_agent.runtime_sandbox_oss,
        "_DEFAULT_TASK_ATTACHMENT_CACHE",
        CancelledTaskAttachmentCache(),
    )
    message = Msg("user", "识别一下图片", "user")
    request_context = {
        "sandbox_context": {"task_id": "task_001", "context_id": "ctx_001"},
        "attachments_manifest": [
            {"file_id": "file_001", "source": "current_task"},
        ],
    }

    with pytest.raises(asyncio.CancelledError):
        await react_agent._append_runtime_attachment_content_parts(
            message,
            request_context,
        )


@pytest.mark.asyncio
async def test_runtime_attachment_media_processing_does_not_insert_local_path(tmp_path) -> None:
    message_processing = importlib.import_module(
        "qwenpaw.agents.utils.message_processing",
    )
    local_path = tmp_path / "photo.png"
    local_path.write_bytes(b"png")
    message = Msg(
        "user",
        [
            {"type": "text", "text": "识别一下图片"},
            {
                "type": "image",
                "source": {"type": "url", "url": local_path.resolve().as_uri()},
                "_runtime_sandbox_attachment": True,
                "_runtime_attachment_file_id": "file_001",
            },
        ],
        "user",
    )

    await message_processing.process_file_and_media_blocks_in_message(message)

    assert isinstance(message.content, list)
    assert not any(
        isinstance(block, dict)
        and block.get("type") == "text"
        and str(local_path) in str(block.get("text", ""))
        for block in message.content
    )


def test_runtime_attachment_file_fixup_uses_file_id_without_local_path(tmp_path) -> None:
    model_factory = importlib.import_module("qwenpaw.agents.model_factory")
    local_path = tmp_path / "cline-rules.md"
    local_path.write_text("# rules", encoding="utf-8")
    items = [
        {
            "type": "file",
            "filename": "cline-rules.md",
            "source": {"type": "url", "url": local_path.resolve().as_uri()},
            "_runtime_sandbox_attachment": True,
            "_runtime_attachment_file_id": "file_001",
        },
    ]

    model_factory._fixup_media_list(items)

    assert items[0]["type"] == "text"
    assert "cline-rules.md" in items[0]["text"]
    assert "file_001" in items[0]["text"]
    assert "runtime_attachment_read" in items[0]["text"]
    assert str(local_path) not in items[0]["text"]
    assert "file://" not in items[0]["text"]


def test_runtime_attachment_media_fixup_drops_internal_markers(tmp_path) -> None:
    model_factory = importlib.import_module("qwenpaw.agents.model_factory")
    local_path = tmp_path / "photo.png"
    local_path.write_bytes(b"png")
    items = [
        {
            "type": "image",
            "source": {"type": "url", "url": local_path.resolve().as_uri()},
            "_runtime_sandbox_attachment": True,
            "_runtime_attachment_file_id": "file_001",
        },
    ]

    model_factory._fixup_media_list(items)

    assert items[0]["type"] == "image"
    assert items[0]["source"]["url"] == str(local_path.resolve())
    assert "_runtime_sandbox_attachment" not in items[0]
    assert "_runtime_attachment_file_id" not in items[0]
