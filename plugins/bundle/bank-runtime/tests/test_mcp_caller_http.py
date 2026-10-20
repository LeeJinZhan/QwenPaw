"""Real Streamable HTTP transport and SDK request-context contract."""
from __future__ import annotations

import asyncio
import json
import socket

import pytest
import uvicorn
from agentscope.message import ToolResultState
from agentscope.tool import ToolResponse
from mcp.server.fastmcp import Context, FastMCP

from qwenpaw.drivers.handlers.mcp_stateful_client import HttpStatefulClient
from qwenpaw.drivers.mcp_context import current_mcp_metadata
from test_mcp_caller_context import admit, make_middleware


@pytest.mark.asyncio
async def test_streamable_http_sdk_receives_per_call_context_with_shared_connection(monkeypatch):
    mcp = FastMCP("caller-context-test", json_response=True)
    received = []
    bodies = []

    @mcp.tool()
    def inspect_caller(keyword: str, ctx: Context) -> dict:
        meta = ctx.request_context.meta
        value = meta.model_dump().get("bank_runtime_context", {}) if meta else {}
        received.append({"keyword": keyword, "context": value})
        return {"received": True}

    app = mcp.streamable_http_app()

    async def capture(scope, receive, send):
        chunks = []
        async def read():
            message = await receive()
            if message["type"] == "http.request":
                chunks.append(message.get("body", b""))
                if not message.get("more_body") and b"".join(chunks):
                    bodies.append(json.loads(b"".join(chunks)))
            return message
        await app(scope, read, send)

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(capture, log_level="error", lifespan="on"))
        server_task = asyncio.create_task(server.serve(sockets=[listener]))
        client = HttpStatefulClient("caller-test", "streamable_http", f"http://127.0.0.1:{port}/mcp")
        try:
            async with asyncio.timeout(10):
                while not server.started:
                    if server_task.done():
                        await server_task
                        pytest.fail("MCP server stopped before startup")
                    await asyncio.sleep(0.01)
            await client.connect()
            tools = await client.list_tools()
            assert set(tools[0].inputSchema["properties"]) == {"keyword"}

            async def invoke(user):
                middleware, _ = make_middleware(monkeypatch, user)
                _, call = await admit(middleware, name="Business__inspect_caller", arguments={"keyword": user})
                async def execute():
                    response = await client.call_tool("inspect_caller", {"keyword": user})
                    assert not response.isError
                    yield ToolResponse(id=call.id, content=[], state=ToolResultState.SUCCESS)
                async for _ in middleware.on_acting(None, {"tool_call": call}, execute):
                    assert current_mcp_metadata() is None

            await asyncio.gather(invoke("a"), invoke("b"))
            # A later non-bank call on the SAME connection must have no identity.
            await client.call_tool("inspect_caller", {"keyword": "ordinary"})
            by_keyword = {item["keyword"]: item["context"] for item in received}
            for user in ("a", "b"):
                assert by_keyword[user]["user_id"] == user
                assert by_keyword[user]["assistant_code"] == "assistant-" + user
                assert by_keyword[user]["org_name"] == "测试机构"
                assert by_keyword[user]["role_names"] == ["客户经理", "业务主管"]
            assert by_keyword["ordinary"] == {}
            calls = [body for body in bodies if body.get("method") == "tools/call"]
            assert len(calls) == 3
            for body in calls:
                params = body["params"]
                keyword = params["arguments"]["keyword"]
                assert params["arguments"] == {"keyword": keyword}
                if keyword != "ordinary":
                    assert params["_meta"]["bank_runtime_context"]["user_id"] == keyword
            assert all("bank_runtime_context" not in body.get("params", {}).get("_meta", {})
                       for body in bodies if body.get("method") != "tools/call")
        finally:
            try:
                await client.close()
            finally:
                server.should_exit = True
                await asyncio.wait_for(server_task, 10)
