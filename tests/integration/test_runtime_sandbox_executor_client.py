from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from qwenpaw.agents.sandbox_executor_client import (
    RuntimeSandboxExecutorClient,
    SandboxExecutorClientError,
)
from qwenpaw.agents.tools.browser_control import browser_use
from qwenpaw.agents.tools.file_search import glob_search
from qwenpaw.agents.tools.file_io import write_file
from qwenpaw.agents.tools.shell import execute_shell_command
from qwenpaw.config.context import (
    current_runtime_sandbox_context,
    current_runtime_tool_execution,
    current_runtime_tool_gateway,
)


class _FakeClient:
    def __init__(self, result: dict) -> None:
        self.execute = AsyncMock(return_value=result)


def test_runtime_executor_uses_service_token_instead_of_gateway_token(monkeypatch) -> None:
    monkeypatch.setenv("QWENPAW_SERVICE_TOKEN", "runtime-service-token")
    sandbox_token = current_runtime_sandbox_context.set({"task_id": "task-a"})
    gateway_token = current_runtime_tool_gateway.set(
        {"base_url": "http://runtime", "token": "worker-session-token"},
    )
    execution_token = current_runtime_tool_execution.set(
        {
            "tool_call_id": "call-a",
            "tool_name": "execute_shell_command",
            "tool_input": {"command": "pwd"},
        },
    )
    try:
        client = RuntimeSandboxExecutorClient.from_current_context()
    finally:
        current_runtime_tool_execution.reset(execution_token)
        current_runtime_tool_gateway.reset(gateway_token)
        current_runtime_sandbox_context.reset(sandbox_token)

    assert client is not None
    assert client.token == "runtime-service-token"


def test_runtime_executor_prefers_worker_reachable_runtime_url(monkeypatch) -> None:
    monkeypatch.setenv("QWENPAW_SERVICE_TOKEN", "runtime-service-token")
    monkeypatch.setenv("QWENPAW_RUNTIME_BASE_URL", "http://runtime-api:8765/")
    sandbox_token = current_runtime_sandbox_context.set({"task_id": "task-a"})
    gateway_token = current_runtime_tool_gateway.set(
        {"base_url": "http://127.0.0.1:8765", "token": "worker-session-token"},
    )
    execution_token = current_runtime_tool_execution.set(
        {
            "tool_call_id": "call-a",
            "tool_name": "execute_shell_command",
            "tool_input": {"command": "pwd"},
        },
    )
    try:
        client = RuntimeSandboxExecutorClient.from_current_context()
    finally:
        current_runtime_tool_execution.reset(execution_token)
        current_runtime_tool_gateway.reset(gateway_token)
        current_runtime_sandbox_context.reset(sandbox_token)

    assert client is not None
    assert client.base_url == "http://runtime-api:8765"


def test_runtime_executor_rejects_missing_service_token(monkeypatch) -> None:
    monkeypatch.delenv("QWENPAW_SERVICE_TOKEN", raising=False)
    monkeypatch.delenv("RUNTIME_QWENPAW_SERVICE_TOKEN", raising=False)
    sandbox_token = current_runtime_sandbox_context.set({"task_id": "task-a"})
    gateway_token = current_runtime_tool_gateway.set(
        {"base_url": "http://runtime", "token": "worker-session-token"},
    )
    execution_token = current_runtime_tool_execution.set(
        {
            "tool_call_id": "call-a",
            "tool_name": "execute_shell_command",
            "tool_input": {"command": "pwd"},
        },
    )
    try:
        with pytest.raises(SandboxExecutorClientError, match="service token"):
            RuntimeSandboxExecutorClient.from_current_context()
    finally:
        current_runtime_tool_execution.reset(execution_token)
        current_runtime_tool_gateway.reset(gateway_token)
        current_runtime_sandbox_context.reset(sandbox_token)


@pytest.mark.asyncio
async def test_shell_routes_to_runtime_executor_without_local_subprocess() -> None:
    client = _FakeClient({"exit_code": 0, "stdout": "sandbox-ok", "stderr": ""})
    with (
        patch.object(RuntimeSandboxExecutorClient, "from_current_context", return_value=client),
        patch("asyncio.create_subprocess_shell", side_effect=AssertionError("host shell must not run")),
    ):
        response = await execute_shell_command("whoami")

    client.execute.assert_awaited_once_with("shell.exec")
    assert response.content[0]["text"] == "sandbox-ok"


@pytest.mark.asyncio
async def test_shell_runtime_executor_failure_propagates_to_gateway_boundary() -> None:
    client = _FakeClient({})
    client.execute.side_effect = SandboxExecutorClientError("Runtime sandbox operation failed")
    with patch.object(RuntimeSandboxExecutorClient, "from_current_context", return_value=client):
        with pytest.raises(SandboxExecutorClientError):
            await execute_shell_command("pwd")


@pytest.mark.asyncio
async def test_glob_runtime_executor_failure_propagates_to_gateway_boundary() -> None:
    client = _FakeClient({})
    client.execute.side_effect = SandboxExecutorClientError("Runtime sandbox operation failed")
    with patch.object(RuntimeSandboxExecutorClient, "from_current_context", return_value=client):
        with pytest.raises(SandboxExecutorClientError):
            await glob_search("**/*.md")


@pytest.mark.asyncio
async def test_file_write_routes_to_runtime_executor_without_host_write() -> None:
    client = _FakeClient({"path": "output/report.md", "bytes": 4})
    with (
        patch.object(RuntimeSandboxExecutorClient, "from_current_context", return_value=client),
        patch("aiofiles.open", side_effect=AssertionError("host file write must not run")),
    ):
        response = await write_file("report.md", "safe")

    client.execute.assert_awaited_once_with("file.write")
    assert "output/report.md" in response.content[0]["text"]


@pytest.mark.asyncio
async def test_browser_routes_to_runtime_executor_without_host_browser_state() -> None:
    client = _FakeClient({"action": "start", "status": "completed"})
    with patch.object(RuntimeSandboxExecutorClient, "from_current_context", return_value=client):
        response = await browser_use("start")

    client.execute.assert_awaited_once_with("browser.execute")
    assert "completed" in response.content[0]["text"]


@pytest.mark.asyncio
async def test_browser_preserves_content_blocks_returned_by_task_daemon() -> None:
    content = [{"type": "text", "text": "sandbox browser result"}]
    client = _FakeClient({"content": content, "text": "sandbox browser result"})
    with patch.object(RuntimeSandboxExecutorClient, "from_current_context", return_value=client):
        response = await browser_use("snapshot")

    assert response.content == content
