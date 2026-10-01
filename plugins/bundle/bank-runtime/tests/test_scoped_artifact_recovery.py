"""Operation recovery through real middleware admission and execution paths."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from agentscope.message import ToolCallBlock, ToolResultState
from agentscope.model import ChatResponse
from agentscope.permission import PermissionBehavior

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bank_runtime.gateway.client import GatewayError, _response_error
from bank_runtime.gateway.middleware import BankRuntimeGatewayMiddleware, GatewayPermissionEngine
from test_gateway_middleware import _Client, _DelegateEngine


async def forbidden(**kwargs):
    pytest.fail("Runtime tools must not execute locally")
    yield


async def execute(middleware, name, payload):
    call = ToolCallBlock(id="call-test", name=name, input=json.dumps(payload))
    return [item async for item in middleware.on_acting(None, {"tool_call": call}, forbidden)]


def engine_for(middleware):
    return GatewayPermissionEngine(_DelegateEngine(PermissionBehavior.ALLOW, middleware.client.events), middleware)


@pytest.mark.asyncio
async def test_independent_docx_and_chart_validation_errors_both_allow_correction():
    class Client(_Client):
        async def preflight(self, name, payload, **kwargs):
            if "content" not in payload:
                raise GatewayError("content missing", code="ARTIFACT_VALIDATION_FAILED")
            return await super().preflight(name, payload, **kwargs)
    middleware = BankRuntimeGatewayMiddleware(Client())
    engine = engine_for(middleware)
    for name, payload in [("artifact_generate", {"artifact_type": "docx"}),
                          ("chart_export", {"chart_id": "chart-a", "format": "png"})]:
        assert (await engine.check_permission(SimpleNamespace(name=name), payload)).behavior == PermissionBehavior.DENY
    called = []
    async def model(**kwargs):
        called.append(True)
        return ChatResponse(content=[ToolCallBlock(id="fixed", name="artifact_generate", input=json.dumps({
            "artifact_type": "docx", "content": {"paragraphs": ["report"]},
        }))], is_last=True)
    await middleware.on_model_call(None, {}, model)
    assert called == [True]
    assert (await engine.check_permission(SimpleNamespace(name="chart_export"), {
        "chart_id": "chart-a", "format": "png", "content": {},
    })).behavior == PermissionBehavior.ALLOW


@pytest.mark.asyncio
async def test_repaired_execution_resolves_old_payload_failure_only_for_same_operation():
    class Client(_Client):
        async def execute_runtime_tool(self, preflight, name, payload):
            if payload.get("content") == "wrong":
                return {"status": "failed", "execution_status": "not_started", "error_code": "ARTIFACT_VALIDATION_FAILED", "result": {"artifact_status": "failed"}}
            return {"status": "success", "result": {"artifact_status": "succeeded", "generated_file_ids": ["file-done"]}}
    middleware = BankRuntimeGatewayMiddleware(Client())
    engine = engine_for(middleware)
    for content in ("wrong", {"paragraphs": ["corrected"]}):
        payload = {"artifact_type": "docx", "output_name": "report.docx", "content": content}
        assert (await engine.check_permission(SimpleNamespace(name="artifact_generate"), payload)).behavior == PermissionBehavior.ALLOW
        await execute(middleware, "artifact_generate", payload)
    assert not middleware.unresolved_file_operations
    middleware._check_file_completion()


@pytest.mark.asyncio
async def test_failed_execution_is_not_erased_by_a_changed_payload_success():
    class Client(_Client):
        async def execute_runtime_tool(self, preflight, name, payload):
            if payload.get("content") == "first":
                return {"status": "failed", "execution_status": "failed", "error_code": "ARTIFACT_VALIDATION_FAILED",
                        "result": {"artifact_status": "failed"}}
            return {"status": "success", "result": {"artifact_status": "succeeded", "generated_file_ids": ["file-done"]}}
    middleware = BankRuntimeGatewayMiddleware(Client())
    engine = engine_for(middleware)
    for content in ("first", "changed"):
        payload = {"artifact_type": "docx", "output_name": "report.docx", "content": content}
        assert (await engine.check_permission(SimpleNamespace(name="artifact_generate"), payload)).behavior == PermissionBehavior.ALLOW
        await execute(middleware, "artifact_generate", payload)
    assert len(middleware.unresolved_file_operations) == 1
    with pytest.raises(Exception) as caught:
        middleware._check_file_completion()
    assert caught.value.error_code == "ARTIFACT_OUTPUT_MISSING"


@pytest.mark.asyncio
async def test_exhausted_ppt_renderer_blocks_same_operation_but_docx_can_execute():
    class Client(_Client):
        executions = 0
        async def execute_runtime_tool(self, preflight, name, payload):
            self.executions += 1
            if payload["artifact_type"] == "pptx":
                raise _response_error({"code": "ARTIFACT_VALIDATION_FAILED", "details": {
                    "reason": "presentation_layout_capacity", "retryable": False,
                    "page_index": 2, "element": "title",
                }}, "failed")
            return {"status": "success", "result": {"artifact_status": "succeeded", "generated_file_ids": ["docx-done"]}}
    middleware = BankRuntimeGatewayMiddleware(Client())
    engine = engine_for(middleware)
    pptx = {"artifact_type": "pptx", "output_name": "slides.pptx"}
    await engine.check_permission(SimpleNamespace(name="artifact_generate"), pptx)
    with pytest.raises(GatewayError):
        await execute(middleware, "artifact_generate", pptx)
    assert (await engine.check_permission(SimpleNamespace(name="artifact_generate"), pptx)).behavior == PermissionBehavior.DENY
    docx = {"artifact_type": "docx", "output_name": "notes.docx"}
    assert (await engine.check_permission(SimpleNamespace(name="artifact_generate"), docx)).behavior == PermissionBehavior.ALLOW
    response = await execute(middleware, "artifact_generate", docx)
    assert response[0].state == ToolResultState.SUCCESS
    assert middleware.client.executions == 2


@pytest.mark.asyncio
async def test_unknown_execution_never_repeats_when_model_changes_input():
    class Client(_Client):
        executions = 0
        async def execute_runtime_tool(self, *args):
            self.executions += 1
            raise GatewayError("timeout", failure_metadata={"execution_status": "execution_unknown", "retryable": False})
    middleware = BankRuntimeGatewayMiddleware(Client())
    engine = engine_for(middleware)
    payload = {"artifact_type": "docx", "output_name": "report.docx", "content": "first"}
    await engine.check_permission(SimpleNamespace(name="artifact_generate"), payload)
    with pytest.raises(GatewayError) as caught:
        await execute(middleware, "artifact_generate", payload)
    assert caught.value.failure_metadata["execution_status"] == "execution_unknown"
    changed = {**payload, "content": "changed"}
    assert (await engine.check_permission(SimpleNamespace(name="artifact_generate"), changed)).behavior == PermissionBehavior.DENY
    assert middleware.client.executions == 1
    assert middleware.unresolved_file_operations
