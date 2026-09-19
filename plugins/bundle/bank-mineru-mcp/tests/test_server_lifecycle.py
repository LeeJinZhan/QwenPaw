from __future__ import annotations

from datetime import timedelta
import socket

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
import pytest

from bank_mineru_mcp.config import MinerUSettings
from bank_mineru_mcp.server import MinerUMcpService
from bank_mineru_mcp.tools import ToolContractError
from bank_mineru_mcp.recovery import recovery_hint


class _Client:
    def __init__(self) -> None:
        self.probes = 0
        self.closes = 0

    async def probe(self):
        self.probes += 1
        return {"status": "healthy"}

    async def close(self):
        self.closes += 1


class _Tools:
    async def parse_documents(
        self, documents, parse_method="auto", language="auto", options=None
    ):
        del documents, parse_method, language, options
        return {"status": "completed", "items": []}

    def read_document_chunks(self, document_ref, cursor=None, limit=5):
        del document_ref, cursor, limit
        return {"chunks": [], "next_cursor": None, "has_more": False}

    def read_range(self, document_ref, **kwargs):
        del document_ref, kwargs
        return {"markdown": "", "has_more": False}

    def aggregate(self, document_ref, ops):
        del document_ref, ops
        return {"results": []}

    def search(self, document_ref, query, sheet=None, limit=100):
        del document_ref, query, sheet, limit
        return {"hits": []}


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _settings(port: int) -> MinerUSettings:
    return MinerUSettings(
        base_url="http://mineru.test",
        submit_mode="file_parse",
        token="secret",
        mcp_port=port,
    )


@pytest.mark.asyncio
async def test_service_exposes_exact_native_mcp_tools_and_stops_idempotently() -> None:
    client = _Client()
    service = MinerUMcpService(
        settings=_settings(_port()),
        tool_service=_Tools(),
        mineru_client=client,
    )

    await service.start()
    await service.start()
    async with streamablehttp_client(
        f"http://127.0.0.1:{service.settings.mcp_port}/mcp",
        timeout=5,
    ) as (read_stream, write_stream, _):
        async with ClientSession(
            read_stream,
            write_stream,
            read_timeout_seconds=timedelta(seconds=5),
        ) as session:
            await session.initialize()
            tools = await session.list_tools()
    assert [tool.name for tool in tools.tools] == [
        "parse_documents",
        "read_document_chunks",
        "read_range",
        "aggregate",
        "search",
    ]
    assert client.probes == 1

    await service.stop()
    await service.stop()
    assert client.closes == 1


@pytest.mark.asyncio
async def test_native_mcp_failure_carries_only_structured_reason_not_private_exception():
    class FailedTools(_Tools):
        def read_document_chunks(self, document_ref, cursor=None, limit=5):
            raise ToolContractError("DOCUMENT_REF_EXPIRED", "private /srv/files/token=secret")
    service = MinerUMcpService(settings=_settings(_port()), tool_service=FailedTools(), mineru_client=_Client())
    await service.start()
    try:
        async with streamablehttp_client(f"http://127.0.0.1:{service.settings.mcp_port}/mcp", timeout=5) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream, read_timeout_seconds=timedelta(seconds=5)) as session:
                await session.initialize()
                result = await authorized_call(session, "read_document_chunks", {"document_ref": "expired"})
        assert result.structuredContent == {
            "status": "failed",
            "error_code": "DOCUMENT_REF_EXPIRED",
            "recovery_hint": recovery_hint("DOCUMENT_REF_EXPIRED"),
        }
        assert "private" not in str(result)
        assert "secret" not in str(result)
    finally:
        await service.stop()

@pytest.mark.asyncio
@pytest.mark.parametrize('name,arguments', [
    ('parse_documents', {'documents': [{'file_id': 'f', 'file_ref': 'r'}]}),
    ('read_document_chunks', {'document_ref': 'r'}),
    ('read_range', {'document_ref': 'r'}),
    ('aggregate', {'document_ref': 'r', 'ops': [{}]}),
    ('search', {'document_ref': 'r', 'query': 'a'}),
])
async def test_every_tool_returns_safe_actionable_recovery_hint(name, arguments, monkeypatch):
    class FailedTools(_Tools):
        async def parse_documents(self, *args, **kwargs):
            raise ToolContractError('DOCUMENT_ARGUMENT_INVALID', 'private /srv/secret-token')
        def fail(self, *args, **kwargs):
            raise ToolContractError('DOCUMENT_ARGUMENT_INVALID', 'private /srv/secret-token')
        read_document_chunks = read_range = aggregate = search = fail
    service = MinerUMcpService(settings=_settings(_port()), tool_service=FailedTools(), mineru_client=_Client())
    # FastMCP's registered functions are exercised with its normal schema conversion.
    from types import SimpleNamespace
    from bank_runtime.gateway.document_access import approved_document_call
    with approved_document_call("task_001", name, arguments) as metadata:
        monkeypatch.setattr(service.mcp, "get_context", lambda: SimpleNamespace(request_context=SimpleNamespace(meta=metadata)))
        content, result = await service.mcp.call_tool(name, arguments)
    assert result['error_code'] == 'DOCUMENT_ARGUMENT_INVALID'
    assert result.get('recovery_hint') and result['recovery_hint'] != 'restart_null'
    assert 'private' not in str(result) and 'secret-token' not in str(result)


async def authorized_call(session, name, arguments):
    from bank_runtime.gateway.document_access import approved_document_call
    with approved_document_call("task_001", name, arguments) as metadata:
        return await session.call_tool(name, arguments, meta=metadata)
