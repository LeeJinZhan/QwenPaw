"""Shared listener must reject foreign, unapproved, expired and revoked refs."""
import json
import socket
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from agentscope.message import ToolCallBlock, ToolResultState
from agentscope.tool import ToolResponse
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from bank_mineru_mcp.config import MinerUSettings
from bank_mineru_mcp.document_store import DocumentStore
from bank_mineru_mcp.server import MinerUMcpService
from bank_mineru_mcp.structured_store import StructuredStore
from bank_mineru_mcp.tools import MinerUToolService, ToolContractError
from bank_runtime.gateway.document_access import (
    DocumentAccessError, approved_document_call, consume_document_call, current_document_task,
)
from bank_runtime.gateway.middleware import BankRuntimeGatewayMiddleware
from bank_runtime.gateway.client import GatewayError
from bank_runtime.sandbox.file_refs import FileRefRegistry
from bank_runtime.sandbox.cache import PreparedSandboxFile
from qwenpaw.drivers.adapters.agentscope_tool import _blocks_from_value
from qwenpaw.drivers.mcp_context import current_mcp_metadata


class Parser:
    async def probe(self): pass
    async def close(self): pass
    async def parse(self, files, **kwargs):
        return {"results": {"file": {"md_content": "授权正文" * 2000}}}, {files[0].file_id: "file"}


class Gateway:
    def __init__(self, task_id):
        self.config = SimpleNamespace(task_id=task_id)
        self.events = []
    async def report_guard(self, preflight, decision):
        self.events.append(("guard", decision))
    async def report_result(self, call_id, status, duration_ms, error_code=""):
        self.events.append(("result", status, error_code))


@pytest.mark.asyncio
@pytest.mark.parametrize("extension", ["csv", "pdf"])
@pytest.mark.parametrize("revocation", ["revoke", "expire", "change"])
async def test_real_mcp_current_task_authorization_and_source_lifecycle(tmp_path, extension, revocation):
    now = datetime.now(timezone.utc)
    clock = [now]
    registry = FileRefRegistry(root=tmp_path, clock=lambda: clock[0])
    root = tmp_path / "task_a"
    root.mkdir()
    path = root / ("file." + extension)
    path.write_bytes(b"id,amount\n1,20\n2,30\n" if extension == "csv" else b"%PDF-1.7\ntest")
    mime = "text/csv" if extension == "csv" else "application/pdf"
    file_ref = registry.issue(PreparedSandboxFile(file_id="file", local_path=path,
        content_type=mime, size_bytes=path.stat().st_size, original_name=path.name,
        expires_at="", task_id="task_a"), expires_at=now + timedelta(minutes=10))
    parser = Parser()
    tools = MinerUToolService(file_resolver=registry, mineru_client=parser,
        document_store=DocumentStore(root=tmp_path), structured_store=StructuredStore(root=tmp_path))
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]
    server = MinerUMcpService(settings=MinerUSettings(base_url="http://unused.test", submit_mode="file_parse", token="test", mcp_port=port),
        mineru_client=parser, tool_service=tools)
    parse_args = {"documents": [{"file_id": "file", "file_ref": file_ref}]}
    with pytest.raises(ToolContractError, match="authorization"):
        await tools.parse_documents(**parse_args)
    await server.start()
    try:
        async with streamablehttp_client(f"http://127.0.0.1:{port}/mcp") as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                async def call(task_id, name, arguments):
                    with approved_document_call(task_id, name, arguments) as metadata:
                        result = await session.call_tool(name, arguments, meta=metadata)
                    return result.structuredContent

                for metadata in (None, {"bank_runtime_document_grant": "forged"}):
                    result = await session.call_tool("parse_documents", parse_args, meta=metadata)
                    assert result.structuredContent["error_code"] == "FILE_ACCESS_DENIED"
                    assert "授权正文" not in str(result)
                assert (await call("task_b", "parse_documents", parse_args))["error_code"] == "FILE_ACCESS_DENIED"

                gateway = Gateway("task_a")
                middleware = BankRuntimeGatewayMiddleware(gateway)
                async def invoke(name, arguments):
                    full_name = "MinerU__" + name
                    middleware.prepare(full_name, middleware.document_input(full_name, arguments), {"tool_call_id": "call"})
                    async def native_handler(**kwargs):
                        actual = json.loads(kwargs["tool_call"].input) if kwargs else arguments
                        result = await session.call_tool(name, actual, meta=current_mcp_metadata())
                        yield ToolResponse(id="call", state=ToolResultState.SUCCESS, content=_blocks_from_value(result))
                    output = [item async for item in middleware.on_acting(None,
                        {"tool_call": ToolCallBlock(id="call", name=full_name, input=json.dumps(arguments))}, native_handler)]
                    return json.loads(output[0].content[0].text)
                parsed = await invoke("parse_documents", parse_args)
                ref = parsed["items"][0]["document_ref"]
                assert gateway.events[-1] == ("result", "completed", "")
                readers = [("read_document_chunks", {"document_ref": ref})]
                if extension == "csv":
                    readers += [("read_range", {"document_ref": ref}),
                                ("read_range", {"document_ref":"file", "rows":["1","2"], "include_header":"true"}),
                                ("aggregate", {"document_ref": ref, "ops": [{"metrics": [{"column": "amount", "fn": "sum"}]}]}),
                                ("search", {"document_ref": ref, "query": "20"})]
                if extension == "csv":
                    bad = {"document_ref":ref,"ops":[{"metrics":[{"column":"amount","op":"sum"}]}]}
                    denied = await call("task_b", "aggregate", bad)
                    assert denied["error_code"] == "FILE_ACCESS_DENIED"
                    assert "argument_error" not in denied
                    alias_success = await invoke("aggregate", bad)
                    assert alias_success["results"][0]["groups"][0]["amount:sum"] == 50
                    conflict = {"document_ref":ref,"ops":[{"metrics":[{"column":"amount","fn":"count","op":"sum"}]}]}
                    failed = await invoke("aggregate", conflict)
                    assert failed["argument_error"]["reason"] == "METRIC_FUNCTION_FIELD"
                    assert "fn" in failed["recovery_hint"]
                    corrected = {"document_ref":ref,"ops":[{"metrics":[{"column":"amount","fn":"sum"}]}]}
                    success = await invoke("aggregate", corrected)
                    assert success['results'][0]['groups'][0]['amount:sum'] == 50
                    assert not middleware.document_reads.pending
                for name, args in readers:
                    assert (await invoke(name, args)).get("status") != "failed"
                    if args["document_ref"] == ref:
                        assert (await call("task_b", name, args))["error_code"] == "FILE_ACCESS_DENIED"
                # Gateway cannot execute a read using an unobserved/foreign handle.
                with pytest.raises(GatewayError) as denied:
                    await invoke("read_document_chunks", {"document_ref": "foreign"})
                assert denied.value.code == "FILE_ACCESS_DENIED"
                assert gateway.events[-1] == ("result", "failed", "FILE_ACCESS_DENIED")
                if revocation == "revoke":
                    if extension == "csv":
                        assert tools._query_result_bytes > 0
                    registry.revoke_task("task_a")
                    assert tools._query_result_bytes == 0
                    assert not tools._query_results
                elif revocation == "expire": clock[0] += timedelta(minutes=11)
                else: path.write_bytes(path.read_bytes() + b"changed")
                for name, args in readers:
                    if args["document_ref"] != ref:
                        continue
                    result = await call("task_a", name, args)
                    assert result["status"] == "failed"
                    assert result["error_code"] in {"FILE_REF_EXPIRED", "FILE_ACCESS_DENIED", "FILE_REF_INVALID"}
                    assert not set(result).intersection({"chunks", "records", "results", "hits"})
    finally:
        await server.stop()


def test_grant_is_one_use_and_bound_to_exact_call(monkeypatch):
    from bank_runtime.gateway import document_access
    args = {"document_ref": "ref"}
    with approved_document_call("task_a", "read_document_chunks", args) as meta:
        with consume_document_call(meta, "read_document_chunks", {**args, "cursor": None, "limit": 5}):
            assert current_document_task() == "task_a"
        with pytest.raises(DocumentAccessError):
            with consume_document_call(meta, "read_document_chunks", args): pass
    with pytest.raises(DocumentAccessError): current_document_task()
    for name, changed in [("search", {**args, "query": "x"}), ("read_document_chunks", {"document_ref": "other"})]:
        with approved_document_call("task_a", "read_document_chunks", args) as meta:
            with pytest.raises(DocumentAccessError):
                with consume_document_call(meta, name, changed): pass
    with approved_document_call("task_a", "read_document_chunks", args) as meta:
        monkeypatch.setattr(document_access.time, "monotonic", lambda: float("inf"))
        with pytest.raises(DocumentAccessError):
            with consume_document_call(meta, "read_document_chunks", args): pass


@pytest.mark.asyncio
async def test_concurrent_grants_do_not_mix_task_contexts():
    import asyncio
    async def run(task_id):
        arguments = {"document_ref": task_id}
        with approved_document_call(task_id, "read_document_chunks", arguments) as metadata:
            with consume_document_call(metadata, "read_document_chunks", arguments):
                await asyncio.sleep(0)
                assert current_document_task() == task_id
        with pytest.raises(DocumentAccessError): current_document_task()
    await asyncio.gather(run("task_a"), run("task_b"))
