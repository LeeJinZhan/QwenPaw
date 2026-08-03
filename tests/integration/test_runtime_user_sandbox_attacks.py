from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from qwenpaw.agents.sandbox_executor_client import RuntimeSandboxExecutorClient
from qwenpaw.agents.tools.file_io import read_file
from qwenpaw.agents.tools.shell import execute_shell_command
from qwenpaw.config.context import (
    current_runtime_sandbox_context,
    current_runtime_tool_execution,
    current_runtime_tool_gateway,
)


@pytest.mark.asyncio
async def test_incomplete_runtime_context_fails_closed_without_host_shell() -> None:
    sandbox_token = current_runtime_sandbox_context.set({"task_id": "task-a"})
    try:
        with patch("asyncio.create_subprocess_shell", side_effect=AssertionError("host shell bypass")):
            response = await execute_shell_command("cat /etc/passwd")
    finally:
        current_runtime_sandbox_context.reset(sandbox_token)
    assert "Runtime task sandbox" in response.content[0]["text"]


@pytest.mark.asyncio
async def test_incomplete_runtime_context_fails_closed_without_host_file_read(tmp_path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("must-not-read", encoding="utf-8")
    sandbox_token = current_runtime_sandbox_context.set({"task_id": "task-a"})
    try:
        response = await read_file(str(secret))
    finally:
        current_runtime_sandbox_context.reset(sandbox_token)
    assert "must-not-read" not in response.content[0]["text"]
    assert "Runtime task sandbox" in response.content[0]["text"]


@pytest.mark.asyncio
async def test_parallel_contextvars_do_not_cross_tool_calls() -> None:
    async def resolve(suffix: str) -> tuple[str, str]:
        sandbox_token = current_runtime_sandbox_context.set({"task_id": f"task-{suffix}"})
        gateway_token = current_runtime_tool_gateway.set(
            {"base_url": "http://runtime", "token": f"token-{suffix}"},
        )
        execution_token = current_runtime_tool_execution.set(
            {
                "tool_call_id": f"call-{suffix}",
                "tool_name": "read_file",
                "tool_input": {"file_path": f"input/{suffix}.txt"},
            },
        )
        try:
            await asyncio.sleep(0)
            client = RuntimeSandboxExecutorClient.from_current_context()
            assert client is not None
            return client.tool_call_id, client.token
        finally:
            current_runtime_tool_execution.reset(execution_token)
            current_runtime_tool_gateway.reset(gateway_token)
            current_runtime_sandbox_context.reset(sandbox_token)

    left, right = await asyncio.gather(resolve("a"), resolve("b"))
    assert left == ("call-a", "token-a")
    assert right == ("call-b", "token-b")
