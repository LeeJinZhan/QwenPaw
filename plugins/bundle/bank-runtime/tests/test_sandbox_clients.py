from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace

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


def _scope():
    return SandboxRequestScope.from_request(
        SimpleNamespace(
            runtime_task_id="task_001",
            sandbox_context={
                "context_id": "ctx_001",
                "task_id": "task_001",
                "signature": "signed",
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
