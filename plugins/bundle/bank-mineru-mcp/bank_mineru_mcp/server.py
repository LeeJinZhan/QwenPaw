"""Managed loopback Streamable HTTP MCP server."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
import uvicorn

from .config import MinerUSettings
from .document_store import DocumentStore
from .mineru_client import MinerUHttpClient
from .tools import MinerUToolService, ToolContractError


class MinerUMcpService:
    def __init__(
        self,
        *,
        settings: MinerUSettings,
        tool_service: MinerUToolService,
        mineru_client: Any,
    ) -> None:
        self.settings = settings
        self.tool_service = tool_service
        self.mineru_client = mineru_client
        self.mcp = FastMCP(
            name="Bank MinerU",
            instructions="Parse only Runtime-authorized opaque file references.",
            host=settings.mcp_host,
            port=settings.mcp_port,
            streamable_http_path="/mcp",
            stateless_http=True,
            json_response=True,
            log_level="WARNING",
        )
        self._register_tools()
        self._server: uvicorn.Server | None = None
        self._server_task: asyncio.Task[None] | None = None
        self._started = False
        self._closed = False

    def _register_tools(self) -> None:
        @self.mcp.tool(
            name="parse_documents",
            description=(
                "Parse 1-5 Runtime-authorized documents with MinerU. "
                "Accepts only opaque file_ref values, never paths or URLs."
            ),
            structured_output=True,
        )
        async def parse_documents(
            documents: list[dict[str, Any]],
            parse_method: str = "auto",
            language: str = "auto",
            options: dict[str, bool] | None = None,
        ) -> dict[str, Any]:
            try:
                return await self.tool_service.parse_documents(
                    documents,
                    parse_method=parse_method,
                    language=language,
                    options=options,
                )
            except ToolContractError as exc:
                raise ValueError(f"{exc.code}: {exc}") from exc

        @self.mcp.tool(
            name="read_document_chunks",
            description="Read bounded chunks from an opaque task-local document_ref.",
            structured_output=True,
        )
        async def read_document_chunks(
            document_ref: str,
            cursor: str | None = None,
            limit: int = 5,
        ) -> dict[str, Any]:
            try:
                return self.tool_service.read_document_chunks(
                    document_ref,
                    cursor=cursor,
                    limit=limit,
                )
            except ToolContractError as exc:
                raise ValueError(f"{exc.code}: {exc}") from exc

    async def start(self) -> None:
        if self._started:
            return
        if self._closed:
            raise RuntimeError("MinerU MCP service cannot restart after stop")
        await self.mineru_client.probe()
        config = uvicorn.Config(
            self.mcp.streamable_http_app(),
            host=self.settings.mcp_host,
            port=self.settings.mcp_port,
            log_level="warning",
            access_log=False,
        )
        self._server = uvicorn.Server(config)
        self._server_task = asyncio.create_task(
            self._server.serve(),
            name="bank-mineru-mcp-server",
        )
        try:
            for _ in range(500):
                if self._server.started:
                    self._started = True
                    return
                if self._server_task.done():
                    await self._server_task
                    raise RuntimeError("MinerU MCP listener stopped during startup")
                await asyncio.sleep(0.01)
            raise TimeoutError("MinerU MCP listener did not become ready")
        except BaseException:
            await self._stop_server()
            await self._close_client()
            self._closed = True
            raise

    async def stop(self) -> None:
        if self._closed:
            return
        await self._stop_server()
        await self._close_client()
        self._closed = True
        self._started = False

    async def _stop_server(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._server_task is not None and not self._server_task.done():
            try:
                await asyncio.wait_for(self._server_task, timeout=10)
            except TimeoutError:
                if self._server is not None:
                    self._server.force_exit = True
                await self._server_task
        elif self._server_task is not None:
            await self._server_task
        self._server = None
        self._server_task = None

    async def _close_client(self) -> None:
        if not self._closed:
            await self.mineru_client.close()

    def cleanup_results(self) -> None:
        self.tool_service.document_store.clear_all()


def build_mineru_mcp_service(
    settings: MinerUSettings,
    *,
    file_resolver: Any,
) -> MinerUMcpService:
    root = Path(
        os.environ.get("QWENPAW_TASK_FILE_ROOT") or "/tmp/qwenpaw-runtime-task-files"
    )
    client = MinerUHttpClient(settings)
    store = DocumentStore(
        root=root,
        max_document_bytes=settings.result_max_bytes,
        max_task_bytes=settings.task_result_max_bytes,
        ttl_seconds=settings.temp_ttl_seconds,
    )
    tools = MinerUToolService(
        file_resolver=file_resolver,
        mineru_client=client,
        document_store=store,
        inline_max_chars=settings.inline_max_chars,
    )
    return MinerUMcpService(
        settings=settings,
        tool_service=tools,
        mineru_client=client,
    )


__all__ = ["MinerUMcpService", "build_mineru_mcp_service"]
