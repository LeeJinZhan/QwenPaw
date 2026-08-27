from __future__ import annotations

import socket

import pytest

from bank_mineru_mcp.config import MinerUSettings
from bank_mineru_mcp.server import MinerUMcpService
from qwenpaw.drivers.capabilities import DriverInvocation
from qwenpaw.drivers.constants import PROTOCOL_MCP
from qwenpaw.drivers.contracts import DriverCard, DriverPolicy
from qwenpaw.drivers.credentials.store import AsyncCredentialStore
from qwenpaw.drivers.handlers.mcp import MCPDriverHandler, validate_mcp_endpoint
from qwenpaw.drivers.manager import DriverManager


class _Client:
    async def probe(self):
        return {"status": "healthy"}

    async def close(self):
        return None


class _Tools:
    async def parse_documents(
        self,
        documents,
        parse_method="auto",
        language="auto",
        options=None,
    ):
        del documents, parse_method, language, options
        return {"status": "completed", "items": []}

    def read_document_chunks(self, document_ref, cursor=None, limit=5):
        del document_ref, cursor, limit
        return {"chunks": [], "next_cursor": None, "has_more": False}


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.mark.asyncio
async def test_qwen_native_driver_exposes_display_namespace_without_mcp_prefix(
    tmp_path,
) -> None:
    settings = MinerUSettings(
        base_url="http://mineru.test",
        submit_mode="file_parse",
        token="secret",
        mcp_port=_port(),
    )
    server = MinerUMcpService(
        settings=settings,
        tool_service=_Tools(),
        mineru_client=_Client(),
    )
    manager = DriverManager(
        tmp_path / "drivers",
        AsyncCredentialStore(tmp_path / "credentials.yaml"),
    )
    manager.register_handler_type(
        PROTOCOL_MCP,
        MCPDriverHandler,
        endpoint_validator=validate_mcp_endpoint,
    )
    await server.start()
    try:
        await manager.register_driver(
            DriverCard(
                name="mineru",
                protocol=PROTOCOL_MCP,
                endpoint={
                    "transport": "streamable_http",
                    "url": f"http://127.0.0.1:{settings.mcp_port}/mcp",
                },
                config={"display_name": "MinerU"},
                policy=DriverPolicy(default_effect="allow"),
            )
        )
        capabilities = await manager.list_capabilities(kind="tool")
        assert [item.name for item in capabilities] == [
            "parse_documents",
            "read_document_chunks",
        ]
        assert [item.exposure.tool_name for item in capabilities] == [
            "MinerU__parse_documents",
            "MinerU__read_document_chunks",
        ]
        assert all("mcp__" not in item.exposure.tool_name for item in capabilities)
        parse_capability = next(
            item for item in capabilities if item.name == "parse_documents"
        )
        document_schema = parse_capability.input_schema["properties"]["documents"]
        item_schema = document_schema["items"]
        if "$ref" in item_schema:
            definition_name = item_schema["$ref"].rsplit("/", 1)[-1]
            item_schema = parse_capability.input_schema["$defs"][definition_name]
        assert set(item_schema["required"]) == {"file_id", "file_ref"}
        assert set(item_schema["properties"]) == {"file_id", "file_ref"}

        read_capability = next(
            item for item in capabilities if item.name == "read_document_chunks"
        )
        result = await manager.invoke_capability(
            DriverInvocation(
                read_capability.capability_id,
                {"document_ref": "dr1_test"},
                request_context={"channel": "bank-runtime", "user_id": "u001"},
            )
        )
        assert result.ok is True
    finally:
        await manager.shutdown_all()
        await server.stop()
