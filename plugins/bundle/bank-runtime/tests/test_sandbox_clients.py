from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
import asyncio

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from bank_runtime.sandbox import broker as broker_module
from bank_runtime.sandbox import executor as executor_module
from bank_runtime.sandbox.broker import RuntimeFileBroker
from bank_runtime.sandbox.executor import RuntimeSandboxExecutor
from bank_runtime.sandbox.scope import SandboxRequestScope


class _Response:
    status_code = 200

    def __init__(self, data):
        self.data = data

    def json(self):
        return {"data": self.data}


class _AsyncClient:
    calls = []
    response_data = {}

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs, self.kwargs))
        return _Response(dict(self.response_data))


def _read_timeout():
    return executor_module.httpx.Timeout(_AsyncClient.calls[-1][2]["timeout"]).read


def _scope():
    return SandboxRequestScope.from_request(
        SimpleNamespace(
            runtime_task_id="task_001",
            sandbox_context={
                "context_id": "ctx_001",
                "task_id": "task_001",
                "signature": "signed",
                "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=3600)).isoformat(),
            },
            attachments_manifest=[],
        )
    )


@pytest.mark.asyncio
async def test_file_broker_uses_only_fixed_runtime_paths_and_service_token(
    monkeypatch,
) -> None:
    _AsyncClient.calls = []
    _AsyncClient.response_data = {"files": []}
    monkeypatch.setattr(broker_module.httpx, "AsyncClient", _AsyncClient)
    broker = RuntimeFileBroker("https://runtime.internal", "service-secret")

    await broker.search(
        _scope(),
        query="材料",
        content_types=[],
        extensions=[],
        sources=["conversation"],
        limit=20,
    )

    url, kwargs, client_kwargs = _AsyncClient.calls[0]
    assert url == "https://runtime.internal/runtime/internal/sandbox/files/search"
    assert kwargs["headers"]["Authorization"] == "Bearer service-secret"
    assert client_kwargs["follow_redirects"] is False
    assert client_kwargs["trust_env"] is False


@pytest.mark.asyncio
async def test_physical_executor_uses_fixed_endpoint_and_exact_arguments(
    monkeypatch,
) -> None:
    _AsyncClient.calls = []
    _AsyncClient.response_data = {"exit_code": 0, "stdout": "ok", "stderr": ""}
    monkeypatch.setattr(executor_module.httpx, "AsyncClient", _AsyncClient)
    executor = RuntimeSandboxExecutor(
        base_url="https://runtime.internal",
        token="service-secret",
        sandbox_context=_scope().sandbox_context,
    )

    result = await executor.execute(
        tool_call_id="tool_001",
        tool_name="execute_shell_command",
        tool_input={"command": "pwd"},
    )

    assert result["stdout"] == "ok"
    url, kwargs, client_kwargs = _AsyncClient.calls[0]
    assert url == "https://runtime.internal/runtime/internal/sandbox/execute"
    assert kwargs["json"]["operation"] == "shell.exec"
    assert kwargs["json"]["arguments"] == {"command": "pwd"}
    assert client_kwargs["follow_redirects"] is False
    assert client_kwargs["trust_env"] is False


def test_current_browser_is_a_physical_tool():
    assert executor_module.is_physical_tool("browser")
    assert executor_module._operation_for("browser") == "browser.execute"


@pytest.mark.asyncio
@pytest.mark.parametrize("requested,expected", [(None, 305), (60, 65), (1200, 1205), (9999, 1805), (True, 305), (float("nan"), 305)])
async def test_native_command_client_waits_for_requested_bound_plus_grace(monkeypatch, requested, expected):
    _AsyncClient.calls = []
    monkeypatch.setattr(executor_module.httpx, "AsyncClient", _AsyncClient)
    executor = RuntimeSandboxExecutor("https://runtime.internal", "token", _scope().sandbox_context)
    await executor.execute(tool_call_id="call", tool_name="execute_shell_command",
                           tool_input={"command": "pwd", "timeout": requested})
    assert _read_timeout() == expected


@pytest.mark.asyncio
async def test_native_command_client_wait_is_bounded_by_remaining_deadline(monkeypatch):
    _AsyncClient.calls = []
    monkeypatch.setattr(executor_module.httpx, "AsyncClient", _AsyncClient)
    context = _scope().sandbox_context
    context["expires_at"] = (datetime.now(timezone.utc) + timedelta(seconds=10)).isoformat()
    executor = RuntimeSandboxExecutor("https://runtime.internal", "token", context)
    await executor.execute(tool_call_id="call", tool_name="execute_shell_command", tool_input={"command": "pwd"})
    assert 0 < _read_timeout() <= 10


@pytest.mark.asyncio
@pytest.mark.parametrize("expiry", [None, {}, 123, "bad", "2099-01-01T00:00:00", "2000-01-01T00:00:00Z"])
async def test_native_command_client_rejects_invalid_or_expired_deadline_before_http(monkeypatch, expiry):
    _AsyncClient.calls = []
    monkeypatch.setattr(executor_module.httpx, "AsyncClient", _AsyncClient)
    context = _scope().sandbox_context
    context["expires_at"] = expiry
    executor = RuntimeSandboxExecutor("https://runtime.internal", "token", context)
    with pytest.raises(executor_module.SandboxExecutorError):
        await executor.execute(tool_call_id="call", tool_name="execute_shell_command", tool_input={"command": "pwd"})
    assert _AsyncClient.calls == []


@pytest.mark.asyncio
async def test_native_command_client_honors_signed_configured_bounds(monkeypatch):
    _AsyncClient.calls = []
    monkeypatch.setattr(executor_module.httpx, "AsyncClient", _AsyncClient)
    context = {**_scope().sandbox_context, "command_default_timeout_seconds": 90, "command_max_timeout_seconds": 600}
    executor = RuntimeSandboxExecutor("https://runtime.internal", "token", context)
    await executor.execute(tool_call_id="call", tool_name="execute_shell_command", tool_input={"command": "pwd"})
    assert _read_timeout() == 95
    await executor.execute(tool_call_id="call", tool_name="execute_shell_command", tool_input={"command": "pwd", "timeout": 1200})
    assert _read_timeout() == 605


@pytest.mark.asyncio
async def test_document_processing_total_request_cannot_outlive_remaining_task_budget(monkeypatch):
    class SlowClient(_AsyncClient):
        async def post(self, *args, **kwargs):
            await asyncio.sleep(0.08)
            return _Response({"ok": True})
    monkeypatch.setattr(executor_module.httpx, "AsyncClient", SlowClient)
    monkeypatch.setattr(executor_module, "_remaining_seconds", lambda context: 0.05)
    executor = RuntimeSandboxExecutor("https://runtime.internal", "token", _scope().sandbox_context)
    with pytest.raises(executor_module.SandboxExecutorError) as error:
        await executor.process_documents(tool_call_id="call", name="parse_documents", arguments={}, plan={})
    assert error.value.code == "DOCUMENT_EXECUTION_TIMEOUT"


@pytest.mark.asyncio
async def test_source_validation_total_request_is_bounded_by_remaining_task_budget(monkeypatch):
    class SlowClient(_AsyncClient):
        async def post(self, *args, **kwargs):
            await asyncio.sleep(0.08)
            return _Response({"files": [{"file_id": "file", "content_hash": "hash"}]})
    monkeypatch.setattr(executor_module.httpx, "AsyncClient", SlowClient)
    monkeypatch.setattr(executor_module, "_remaining_seconds", lambda context: 0.05)
    executor = RuntimeSandboxExecutor("https://runtime.internal", "token", _scope().sandbox_context)
    with pytest.raises(executor_module.SandboxExecutorError) as error:
        await executor.validate_sources([{"file_id": "file", "sha256": "hash"}])
    assert error.value.code == "DOCUMENT_EXECUTION_TIMEOUT"


@pytest.mark.asyncio
async def test_source_validation_rejects_expired_task_before_http(monkeypatch):
    _AsyncClient.calls = []
    monkeypatch.setattr(executor_module.httpx, "AsyncClient", _AsyncClient)
    executor = RuntimeSandboxExecutor("https://runtime.internal", "token", {**_scope().sandbox_context, "expires_at": "2000-01-01T00:00:00Z"})
    with pytest.raises(executor_module.SandboxExecutorError) as error:
        await executor.validate_sources([{"file_id": "file", "sha256": "hash"}])
    assert error.value.code == "DOCUMENT_REF_EXPIRED"
    assert _AsyncClient.calls == []
