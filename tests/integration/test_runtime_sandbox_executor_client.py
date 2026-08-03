from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from qwenpaw.agents.sandbox_executor_client import RuntimeSandboxExecutorClient
from qwenpaw.agents.tools.browser_control import browser_use
from qwenpaw.agents.tools.file_io import write_file
from qwenpaw.agents.tools.shell import execute_shell_command


class _FakeClient:
    def __init__(self, result: dict) -> None:
        self.execute = AsyncMock(return_value=result)


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
