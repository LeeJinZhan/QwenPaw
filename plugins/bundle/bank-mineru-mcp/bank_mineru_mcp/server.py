"""Managed loopback Streamable HTTP MCP server."""

from __future__ import annotations

import asyncio
import inspect
import os
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field
import uvicorn

from .config import MinerUSettings
from .document_store import DocumentStore
from .mineru_client import build_mineru_client
from .structured_store import StructuredStore
from .tools import MinerUToolService, ToolContractError
from .recovery import recovery_hint


class RuntimeDocumentRef(BaseModel):
    """The paired Runtime identifiers required to resolve one task file."""

    model_config = ConfigDict(extra="forbid")

    file_id: str = Field(min_length=1, description="Runtime uploaded file ID")
    file_ref: str = Field(
        min_length=1,
        description="Opaque task-scoped file reference supplied with the attachment",
    )


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

    async def _authorized_call(self, name, arguments):
        from bank_runtime.gateway.document_access import consume_document_call, DocumentAccessError

        try:
            metadata = self.mcp.get_context().request_context.meta
        except ValueError as exc:
            # Direct invocation outside an MCP request is not authorized.
            raise ToolContractError("FILE_ACCESS_DENIED", "Current task authorization is required") from exc
        try:
            with consume_document_call(metadata, name, arguments):
                if name != "parse_documents" and str(arguments.get("document_ref", "")).startswith("ds1_"):
                    return await self.tool_service.execute_structured_query(name, arguments)
                result = getattr(self.tool_service, name)(**arguments)
                return await result if inspect.isawaitable(result) else result
        except DocumentAccessError as exc:
            raise ToolContractError(exc.code, str(exc)) from exc

    def _register_tools(self) -> None:
        @self.mcp.tool(
            name="parse_documents",
            description=(
                "Parse 1-5 Runtime-authorized documents. XLSX/CSV/TSV use local structured extraction; other formats use MinerU. For structured results use read_range/aggregate/search. "
                "Each document must contain both file_id and its paired opaque "
                "file_ref from the current attachment; never pass paths or URLs."
            ),
            structured_output=True,
        )
        async def parse_documents(
            documents: list[RuntimeDocumentRef],
            parse_method: str = "auto",
            language: str = "auto",
            options: dict[str, bool] | None = None,
        ) -> dict[str, Any]:
            try:
                return await self._authorized_call("parse_documents", {
                    "documents": [document.model_dump() for document in documents],
                    "parse_method": parse_method, "language": language, "options": options,
                })
            except ToolContractError as exc:
                return {"status": "failed", "error_code": exc.code, "recovery_hint": recovery_hint(exc.code), "items": []}

        @self.mcp.tool(
            name="read_document_chunks",
            description=(
                "Read bounded chunks from an opaque task-local document_ref. "
                "Start with cursor=null, then copy next_cursor exactly until "
                "has_more=false. limit is 1-10 (default 5). An omitted cursor "
                "restarts at the beginning. Retrying the same cursor and limit "
                "returns the same page while the document remains valid; "
                "deduplicate chunks by index. Do not infer full-table totals "
                "from previews or incomplete pages. Supports layout and structured results; "
                "structured workbooks use read_range/aggregate/search."
            ),
            structured_output=True,
        )
        async def read_document_chunks(
            document_ref: str,
            cursor: str | None = None,
            limit: int = 5,
        ) -> dict[str, Any]:
            try:
                return await self._authorized_call("read_document_chunks", {"document_ref": document_ref, "cursor": cursor, "limit": limit})
            except ToolContractError as exc:
                return {"status": "failed", "error_code": exc.code, "recovery_hint": recovery_hint(exc.code)}

        @self.mcp.tool(
            name="read_range",
            description=(
                "Read a bounded row range from one sheet of a structured "
                "document_ref (xlsx/csv/tsv). Pass sheet name, rows=[start,end] "
                "or continue with next_row_cursor; optional columns projection; "
                "format cell reads one columns entry and rows=[r,r], with row_cursor=next_cell_cursor for long text. format markdown (header repeated), records, or inventory. Inventory without sheet pages sheet summaries; with sheet pages columns/merges. Continue with next_inventory_cursor as row_cursor. Pages are capped "
                "at 32000 UTF-8 bytes including metadata; echo fields report the served range and sheet "
                "totals. Never infer values outside the echoed range."
            ),
            structured_output=True,
        )
        async def read_range(
            document_ref: str,
            sheet: str | None = None,
            rows: list[int] | None = None,
            row_cursor: int | None = None,
            columns: list[str] | None = None,
            format: str = "markdown",
            include_header: bool = True,
        ) -> dict[str, Any]:
            try:
                return await self._authorized_call("read_range", {"document_ref": document_ref, "sheet": sheet, "rows": rows, "row_cursor": row_cursor, "columns": columns, "format": format, "include_header": include_header})
            except ToolContractError as exc:
                return {"status": "failed", "error_code": exc.code, "recovery_hint": recovery_hint(exc.code)}

        @self.mcp.tool(
            name="aggregate",
            description=(
                "Compute bounded server-side statistics over structured sheets: "
                "ops with sheet, optional group_by, metrics (sum/avg/count/"
                "count_distinct/min/max/median), optional filter and row_range, "
                "or cross_sheet_union by a shared key column. Results include "
                "rows_scanned/rows_matched/full_range so scope can be stated. Use group_cursor=0 in each op for high-cardinality group pages, then next_group_cursor. "
                "honestly. Use this instead of reading every row for totals."
            ),
            structured_output=True,
        )
        async def aggregate(document_ref: str, ops: list[dict[str, Any]]) -> dict[str, Any]:
            try:
                return await self._authorized_call("aggregate", {"document_ref": document_ref, "ops": ops})
            except ToolContractError as exc:
                return {"status": "failed", "error_code": exc.code, "recovery_hint": recovery_hint(exc.code)}

        @self.mcp.tool(
            name="search",
            description=(
                "Locate rows containing a substring (or simple pattern) across "
                "sheets of a structured document_ref; returns sheet, row number "
                "and values, capped at limit (<=100). Use to find relevant rows "
                "before reading ranges."
            ),
            structured_output=True,
        )
        async def search(
            document_ref: str,
            query: str,
            sheet: str | None = None,
            limit: int = 100,
        ) -> dict[str, Any]:
            try:
                return await self._authorized_call("search", {"document_ref": document_ref, "query": query, "sheet": sheet, "limit": limit})
            except ToolContractError as exc:
                return {"status": "failed", "error_code": exc.code, "recovery_hint": recovery_hint(exc.code)}

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
    client = build_mineru_client(settings)
    store = DocumentStore(
        root=root,
        max_document_bytes=settings.result_max_bytes,
        max_task_bytes=settings.task_result_max_bytes,
        ttl_seconds=settings.temp_ttl_seconds,
    )
    structured = StructuredStore(
        root=root,
        max_task_bytes=settings.structured_task_max_bytes,
        max_document_bytes=settings.structured_document_max_bytes,
        ttl_seconds=settings.temp_ttl_seconds,
    )
    tools = MinerUToolService(
        file_resolver=file_resolver,
        mineru_client=client,
        document_store=store,
        structured_store=structured,
        inline_max_chars=settings.inline_max_chars,
        parse_timeout_seconds=settings.parse_timeout_seconds,
        extract_memory_bytes=settings.extract_memory_bytes,
    )
    return MinerUMcpService(
        settings=settings,
        tool_service=tools,
        mineru_client=client,
    )


__all__ = ["MinerUMcpService", "build_mineru_mcp_service"]
