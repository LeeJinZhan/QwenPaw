from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import httpx
import pytest

from bank_mineru_mcp.config import MinerUSettings
from bank_mineru_mcp.mineru_client import (
    MinerUClientError,
    MinerUHttpClient,
    build_mineru_client,
)
from bank_runtime.sandbox.file_refs import ResolvedTaskFile


def _settings(mode: str) -> MinerUSettings:
    return MinerUSettings(
        base_url="http://mineru.test",
        submit_mode=mode,
        token="secret",
        connect_timeout_seconds=1,
        upload_timeout_seconds=5,
        parse_timeout_seconds=5,
        poll_interval_seconds=0.01,
        mcp_host="127.0.0.1",
        mcp_port=18081,
        inline_max_chars=20000,
        result_max_bytes=1024 * 1024,
        task_result_max_bytes=2 * 1024 * 1024,
        temp_ttl_seconds=604800,
    )


def _official_settings() -> MinerUSettings:
    return replace(
        _settings("tasks"),
        provider="official_flash",
        base_url="https://mineru.test/api/v1/agent",
        token="",
    )


def _file(tmp_path: Path) -> ResolvedTaskFile:
    path = tmp_path / "secret-name.pdf"
    path.write_bytes(b"%PDF-1.7\ncontent")
    return ResolvedTaskFile(
        task_id="task_001",
        file_id="file_001",
        path=path,
        media_type="application/pdf",
        extension=".pdf",
        size_bytes=path.stat().st_size,
        sha256="0" * 64,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )


@pytest.mark.asyncio
async def test_file_parse_mode_posts_sanitized_multipart_without_fallback(
    tmp_path,
) -> None:
    requests: list[tuple[str, bytes]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        requests.append((request.url.path, body))
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "healthy", "version": "2"})
        return httpx.Response(
            200,
            json={
                "version": "2",
                "results": {"file_file_001": {"md_content": "# 结果\n正文"}},
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = MinerUHttpClient(_settings("file_parse"), http_client=http)
    await client.probe()
    payload, stems = await client.parse(
        [_file(tmp_path)],
        parse_method="auto",
        language="zh",
        tables=True,
        formulas=True,
    )

    assert stems == {"file_001": "file_file_001"}
    assert payload["results"]["file_file_001"]["md_content"] == "# 结果\n正文"
    assert [path for path, _ in requests] == ["/health", "/file_parse"]
    body = requests[-1][1]
    assert b"file_file_001.pdf" in body
    assert b"secret-name.pdf" not in body
    await client.close()


@pytest.mark.asyncio
async def test_tasks_mode_submits_polls_and_fetches_result(tmp_path) -> None:
    paths: list[str] = []
    status_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal status_calls
        paths.append(request.url.path)
        if request.url.path == "/tasks" and request.method == "POST":
            return httpx.Response(
                202, json={"task_id": "remote-001", "status": "pending"}
            )
        if request.url.path == "/tasks/remote-001":
            status_calls += 1
            return httpx.Response(
                200,
                json={
                    "task_id": "remote-001",
                    "status": "processing" if status_calls == 1 else "completed",
                },
            )
        if request.url.path == "/tasks/remote-001/result":
            return httpx.Response(
                200, json={"results": {"file_file_001": {"md_content": "done"}}}
            )
        return httpx.Response(404)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = MinerUHttpClient(_settings("tasks"), http_client=http)
    result, _ = await client.parse([_file(tmp_path)])
    assert result["results"]["file_file_001"]["md_content"] == "done"
    assert paths == [
        "/tasks",
        "/tasks/remote-001",
        "/tasks/remote-001",
        "/tasks/remote-001/result",
    ]
    await client.close()


@pytest.mark.asyncio
async def test_mode_never_falls_back_and_rejects_oversized_response(tmp_path) -> None:
    paths: list[str] = []

    async def failed(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(503, json={"detail": "unavailable"})

    client = MinerUHttpClient(
        _settings("file_parse"),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(failed)),
    )
    with pytest.raises(MinerUClientError):
        await client.parse([_file(tmp_path)])
    assert paths == ["/file_parse"]
    await client.close()

    async def too_large(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps({"value": "x" * 200}).encode())

    small = replace(_settings("file_parse"), result_max_bytes=64)
    client = MinerUHttpClient(
        small,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(too_large)),
    )
    with pytest.raises(MinerUClientError) as oversized:
        await client.parse([_file(tmp_path)])
    assert oversized.value.code == "DOCUMENT_RESULT_TOO_LARGE"
    await client.close()


@pytest.mark.asyncio
async def test_official_flash_uploads_polls_and_normalizes_markdown(tmp_path) -> None:
    requests: list[tuple[str, str]] = []
    status_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal status_calls
        requests.append((request.method, request.url.path))
        assert "authorization" not in request.headers
        if request.method == "POST" and request.url.path == "/api/v1/agent/parse/file":
            payload = json.loads((await request.aread()).decode())
            assert payload == {
                "file_name": "file_file_001.pdf",
                "language": "ch",
                "is_ocr": True,
                "enable_formula": True,
                "enable_table": True,
            }
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "task_id": "flash-001",
                        "file_url": "https://assets.mineru.test/upload/flash-001",
                    },
                },
            )
        if request.method == "PUT" and request.url.path == "/upload/flash-001":
            assert await request.aread() == b"%PDF-1.7\ncontent"
            return httpx.Response(200)
        if request.url.path == "/api/v1/agent/parse/flash-001":
            status_calls += 1
            data = {
                "task_id": "flash-001",
                # The official SDK treats non-terminal states as transitional;
                # the service can introduce names that are newer than the SDK.
                "state": "waiting_upload" if status_calls == 1 else "done",
            }
            if status_calls > 1:
                data.update(
                    {
                        "markdown_url": "https://assets.mineru.test/result/flash-001.md",
                        "extract_progress": {
                            "extracted_pages": 12,
                            "total_pages": 12,
                        },
                    }
                )
            return httpx.Response(200, json={"code": 0, "data": data})
        if request.url.path == "/result/flash-001.md":
            return httpx.Response(200, text="# 官方结果\n正文")
        return httpx.Response(404)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = build_mineru_client(_official_settings(), http_client=http)

    assert (await client.probe())["provider"] == "official_flash"
    result, stems = await client.parse(
        [_file(tmp_path)],
        parse_method="ocr",
        language="zh",
        tables=True,
        formulas=True,
    )

    assert stems == {"file_001": "file_file_001"}
    assert result == {
        "results": {
            "file_file_001": {
                "md_content": "# 官方结果\n正文",
                "page_count": 12,
            }
        }
    }
    assert requests == [
        ("POST", "/api/v1/agent/parse/file"),
        ("PUT", "/upload/flash-001"),
        ("GET", "/api/v1/agent/parse/flash-001"),
        ("GET", "/api/v1/agent/parse/flash-001"),
        ("GET", "/result/flash-001.md"),
    ]
    await client.close()
