"""Real MCP wire -> native Driver adapter -> read ledger regression."""
import json
import socket
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from openpyxl import Workbook

from bank_mineru_mcp.config import MinerUSettings
from bank_mineru_mcp.document_store import DocumentStore
from bank_mineru_mcp.structured_store import StructuredStore
from bank_mineru_mcp.tools import MinerUToolService
from bank_mineru_mcp.server import MinerUMcpService
from bank_runtime.gateway.document_reads import DocumentReadLedger
from qwenpaw.drivers.adapters.agentscope_tool import _blocks_from_value


class NoLayoutClient:
    async def parse(self, *args, **kwargs):
        raise AssertionError("Excel must never call the MinerU layout engine")

    async def probe(self):
        return {"status": "healthy"}

    async def close(self):
        pass


@pytest.mark.asyncio
async def test_five_sheet_excel_wire_pagination_and_coverage(tmp_path):
    task = tmp_path / "task_test"
    task.mkdir()
    path = task / "planning.xlsx"
    workbook = Workbook()
    workbook.remove(workbook.active)
    expected = {}
    for name, count in zip(("规划总览", "年度规划", "节奏矩阵", "技术底座", "事项清单"), (17, 65, 14, 14, 15)):
        sheet = workbook.create_sheet(name)
        sheet.append(["编号", "说明"])
        for index in range(count):
            sheet.append([index + 1, "技术建设内容" * 80])
        expected[name] = count
    workbook.save(path)
    source = SimpleNamespace(task_id="task_test", file_id="file_test", path=path,
        extension=".xlsx", media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1))
    client = NoLayoutClient()
    service = MinerUToolService(file_resolver=SimpleNamespace(resolve=lambda ref: source),
        mineru_client=client, document_store=DocumentStore(root=tmp_path),
        structured_store=StructuredStore(root=tmp_path))
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = MinerUMcpService(settings=MinerUSettings(base_url="http://unused.test", submit_mode="file_parse", token="test", mcp_port=port),
        tool_service=service, mineru_client=client)
    await server.start()
    try:
        async with streamablehttp_client(f"http://127.0.0.1:{port}/mcp") as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                ledger = DocumentReadLedger()
                async def call(name, payload):
                    ledger.start("MinerU__" + name, payload)
                    from bank_runtime.gateway.document_access import approved_document_call
                    with approved_document_call("task_test", name, payload) as metadata:
                        result = await session.call_tool(name, payload, meta=metadata)
                    blocks = _blocks_from_value(result)
                    assert len(blocks) == 1
                    assert len(blocks[0].text.encode()) <= 32000
                    ledger.observe("MinerU__" + name, payload, blocks, not result.isError)
                    return json.loads(blocks[0].text)
                parsed = await call("parse_documents", {"documents": [{"file_id": "file_test", "file_ref": "authorized-test-ref"}]})
                item = parsed["items"][0]
                assert item["content_mode"] == "structured"
                assert item["inventory"]["engine"] == "ooxml-1"
                ref = item["document_ref"]
                invalid = await call("read_range", {"document_ref": ref, "sheet": "规划总览", "columns": ["编号", "编号"]})
                assert invalid["error_code"] == "DOCUMENT_ARGUMENT_INVALID"
                assert not ledger.documents[ref].complete
                projected = await call("read_range", {"document_ref": ref, "sheet": "规划总览", "columns": ["编号"]})
                assert projected["all_columns"] is False
                assert ledger.documents[ref].covered_rows("规划总览") == 0
                assert ledger.declaration_conflict("所有工作表全量总结") == "DOCUMENT_READ_INCOMPLETE"
                cursor = None
                pages = 0
                while True:
                    payload = {"document_ref": ref, "cursor": cursor, "limit": 10}
                    page = await call("read_document_chunks", payload)
                    assert page == await call("read_document_chunks", payload)
                    pages += 1
                    if not page["has_more"]:
                        break
                    cursor = page["next_cursor"]
                assert pages > 1
                assert not ledger.pending
                assert ledger.documents[ref].complete
                assert {name: ledger.documents[ref].covered_rows(name) for name in expected} == expected
                assert ledger.declaration_conflict("所有工作表全量总结") == ""
                # A restarted store can resume the same authorized reference.
                restarted = StructuredStore(root=tmp_path)
                assert restarted.read_range(ref, sheet="年度规划", rows=[1, 3])["rows_scanned"] == 3
    finally:
        await server.stop()
