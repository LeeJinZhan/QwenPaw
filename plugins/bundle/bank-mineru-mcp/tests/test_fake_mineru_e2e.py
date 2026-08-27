from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import socket

from fastapi import FastAPI, Request
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
import pytest
import uvicorn

from bank_mineru_mcp.config import MinerUSettings
from bank_mineru_mcp.document_store import DocumentStore
from bank_mineru_mcp.mineru_client import MinerUHttpClient
from bank_mineru_mcp.server import MinerUMcpService
from bank_mineru_mcp.tools import MinerUToolService
from bank_runtime.sandbox.cache import PreparedSandboxFile
from bank_runtime.sandbox.file_refs import FileRefRegistry


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@asynccontextmanager
async def _fake_mineru_server(requests: list[str]):
    app = FastAPI()

    @app.get("/health")
    async def health(request: Request) -> dict[str, str]:
        requests.append(request.url.path)
        assert request.headers["authorization"] == "Bearer secret"
        return {"status": "healthy", "version": "fake-http"}

    @app.post("/file_parse")
    async def file_parse(request: Request) -> dict[str, object]:
        requests.append(request.url.path)
        assert request.headers["authorization"] == "Bearer secret"
        body = await request.body()
        assert b"file_file_001.pdf" in body
        assert "敏感原始名称".encode() not in body
        return {
            "version": "fake-http",
            "results": {
                "file_file_001": {
                    "md_content": "# 扫描报告\n" + "识别正文。" * 5000,
                    "page_count": 12,
                }
            },
        }

    port = _port()
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="warning",
            access_log=False,
        )
    )
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(500):
            if server.started:
                break
            if task.done():
                await task
                raise RuntimeError("fake MinerU stopped during startup")
            await asyncio.sleep(0.01)
        else:
            raise TimeoutError("fake MinerU did not start")
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task


@pytest.mark.asyncio
async def test_native_mcp_call_uses_fake_mineru_and_reads_large_result(
    tmp_path,
) -> None:
    task_root = tmp_path / "task_001"
    task_root.mkdir()
    source = task_root / "file_001.pdf"
    source.write_bytes(b"%PDF-1.7\nscanned")
    registry = FileRefRegistry(root=tmp_path, process_start_key=b"f" * 32)
    file_ref = registry.issue(
        PreparedSandboxFile(
            file_id="file_001",
            local_path=source,
            content_type="application/pdf",
            size_bytes=source.stat().st_size,
            original_name="敏感原始名称.pdf",
            expires_at="",
            task_id="task_001",
        ),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    requests: list[str] = []

    async with _fake_mineru_server(requests) as base_url:
        settings = MinerUSettings(
            base_url=base_url,
            submit_mode="file_parse",
            token="secret",
            mcp_port=_port(),
            inline_max_chars=20000,
        )
        mineru = MinerUHttpClient(settings)
        store = DocumentStore(root=tmp_path, process_start_key=b"d" * 32)
        service = MinerUMcpService(
            settings=settings,
            mineru_client=mineru,
            tool_service=MinerUToolService(
                file_resolver=registry,
                mineru_client=mineru,
                document_store=store,
                inline_max_chars=20000,
            ),
        )

        await service.start()
        try:
            async with streamablehttp_client(
                f"http://127.0.0.1:{settings.mcp_port}/mcp",
                timeout=5,
            ) as (read_stream, write_stream, _):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    parsed = await session.call_tool(
                        "parse_documents",
                        {
                            "documents": [
                                {"file_id": "file_001", "file_ref": file_ref}
                            ],
                            "parse_method": "ocr",
                            "language": "zh",
                        },
                    )
                    assert parsed.isError is False
                    item = parsed.structuredContent["items"][0]
                    assert item["content_mode"] == "chunked"
                    assert item["page_count"] == 12
                    assert item["markdown"] is None

                    chunks = await session.call_tool(
                        "read_document_chunks",
                        {"document_ref": item["document_ref"], "limit": 2},
                    )
                    assert chunks.isError is False
                    assert len(chunks.structuredContent["chunks"]) == 2
                    assert chunks.structuredContent["has_more"] is True
        finally:
            await service.stop()

    assert requests == ["/health", "/file_parse"]
