"""Confirmed Runtime failures participate in admission and recovery budgets."""
from types import SimpleNamespace
from pathlib import Path
import sys

import pytest
from agentscope.permission import PermissionBehavior

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bank_runtime.gateway.client import GatewayError
from bank_runtime.gateway.middleware import BankRuntimeGatewayMiddleware
from bank_runtime.gateway.recovery_budget import OperationRecoveryBudget
from test_gateway_middleware import _Client
from test_scoped_artifact_recovery import engine_for, execute


def service_client(path, facts, code="WORKER_UNAVAILABLE"):
    class Client(_Client):
        executions = 0

        async def execute_runtime_tool(self, preflight, name, payload):
            self.executions += 1
            if path == "exception":
                raise GatewayError("confirmed service failure", code=code,
                                   failure_metadata=facts)
            return {"status": "failed", "error_code": code,
                    "details": facts, "result": {"artifact_status": "failed"}}
    return Client()


async def attempt(middleware, engine, payload, path):
    assert (await engine.check_permission(SimpleNamespace(name="artifact_generate"), payload)).behavior == PermissionBehavior.ALLOW
    if path == "exception":
        with pytest.raises(GatewayError):
            await execute(middleware, "artifact_generate", payload)
    else:
        await execute(middleware, "artifact_generate", payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["result", "exception"])
@pytest.mark.parametrize("code", ["WORKER_UNAVAILABLE", "ARTIFACT_VALIDATION_FAILED"])
@pytest.mark.parametrize("facts", [
    {"execution_status": "failed", "retryable": False},
    {"execution_status": "not_started", "retryable": True, "remaining_attempts": 0},
])
async def test_explicit_service_stop_denies_changed_input_but_keeps_independent_operation(path, code, facts):
    client = service_client(path, facts, code)
    middleware = BankRuntimeGatewayMiddleware(client)
    engine = engine_for(middleware)
    payload = {"artifact_type": "docx", "output_name": "report.docx", "content": "first"}
    await attempt(middleware, engine, payload, path)
    assert (await engine.check_permission(SimpleNamespace(name="artifact_generate"),
                                         {**payload, "content": "changed"})).behavior == PermissionBehavior.DENY
    assert (await engine.check_permission(SimpleNamespace(name="artifact_generate"),
                                         {**payload, "output_name": "independent.docx"})).behavior == PermissionBehavior.ALLOW
    assert client.executions == 1
    assert middleware.artifact_recovery.total_failures == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["result", "exception"])
async def test_retryable_service_failures_consume_operation_and_task_budget(path):
    client = service_client(path, {"execution_status": "failed", "retryable": True})
    middleware = BankRuntimeGatewayMiddleware(client)
    middleware.artifact_recovery = OperationRecoveryBudget(task_limit=3)
    engine = engine_for(middleware)
    payload = {"artifact_type": "docx", "output_name": "report.docx", "content": "first"}
    for _ in range(2):
        await attempt(middleware, engine, payload, path)
    assert middleware.artifact_recovery.total_failures == 2
    assert (await engine.check_permission(SimpleNamespace(name="artifact_generate"), payload)).behavior == PermissionBehavior.DENY
    independent = {**payload, "output_name": "independent.docx"}
    await attempt(middleware, engine, independent, path)
    assert middleware.artifact_recovery.total_failures == 3
    assert middleware.artifact_recovery.task_exhausted
    assert middleware.artifact_recovery.pending_input_failures == 0
    assert (await engine.check_permission(SimpleNamespace(name="artifact_generate"),
                                         {**payload, "output_name": "third.docx"})).behavior == PermissionBehavior.DENY


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["result", "exception"])
@pytest.mark.parametrize("code", ["WORKER_UNAVAILABLE", "ARTIFACT_VALIDATION_FAILED"])
async def test_unknown_failure_is_counted_once_and_requires_verification(path, code):
    client = service_client(path, {"execution_status": "execution_unknown", "retryable": False}, code)
    middleware = BankRuntimeGatewayMiddleware(client)
    engine = engine_for(middleware)
    payload = {"artifact_type": "docx", "output_name": "report.docx", "content": "first"}
    await attempt(middleware, engine, payload, path)
    assert middleware.artifact_recovery.total_failures == 1
    assert (await engine.check_permission(SimpleNamespace(name="artifact_generate"),
                                         {**payload, "content": "changed"})).behavior == PermissionBehavior.DENY


@pytest.mark.asyncio
@pytest.mark.parametrize("facts,blocked", [
    (None, False),
    ({"execution_status": "not_started", "retryable": False}, True),
    ({"execution_status": "not_started", "retryable": True, "remaining_attempts": 0}, True),
])
async def test_preflight_validation_preserves_legacy_correction_and_explicit_stop(facts, blocked):
    class Client(_Client):
        calls = 0

        async def preflight(self, name, payload, **kwargs):
            self.calls += 1
            if payload.get("content") == "wrong":
                raise GatewayError("invalid content", code="ARTIFACT_VALIDATION_FAILED",
                                   failure_metadata=facts)
            return await super().preflight(name, payload, **kwargs)
    client = Client()
    middleware = BankRuntimeGatewayMiddleware(client)
    engine = engine_for(middleware)
    payload = {"artifact_type": "docx", "output_name": "report.docx", "content": "wrong"}
    assert (await engine.check_permission(SimpleNamespace(name="artifact_generate"), payload)).behavior == PermissionBehavior.DENY
    corrected = {**payload, "content": {"paragraphs": ["corrected"]}}
    expected = PermissionBehavior.DENY if blocked else PermissionBehavior.ALLOW
    assert (await engine.check_permission(SimpleNamespace(name="artifact_generate"), corrected)).behavior == expected
    assert client.calls == (1 if blocked else 2)
    assert middleware.artifact_recovery.total_failures == 1
