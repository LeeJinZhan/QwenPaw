# -*- coding: utf-8 -*-
"""Tests for the Bubblewrap-backed sandbox shell tool."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from qwenpaw.agents.tools.sandboxed_shell import (
    BubblewrapShellExecutor,
    SandboxProfileConfig,
    execute_sandboxed_shell_command,
)


@pytest.mark.asyncio
async def test_bubblewrap_executor_builds_workspace_only_sandbox(tmp_path):
    """The executor should run the shell inside a workspace-mounted bwrap."""
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    workspace.mkdir()

    proc = MagicMock()
    proc.returncode = 0
    proc.pid = 1234
    proc.communicate = AsyncMock(return_value=(b"ok\n", b""))

    with patch(
        "qwenpaw.agents.tools.sandboxed_shell.shutil.which",
        return_value="/usr/bin/bwrap",
    ), patch(
        "qwenpaw.agents.tools.sandboxed_shell.asyncio.create_subprocess_exec",
        AsyncMock(return_value=proc),
    ) as create_proc:
        executor = BubblewrapShellExecutor(
            SandboxProfileConfig(
                enabled=True,
                profile_id="linux-bubblewrap-workspace",
                writable_roots=[str(workspace)],
                home_dir=str(home),
                allow_network=False,
                shell_executable="/bin/bash",
            ),
        )

        result = await executor.execute(
            "echo ok",
            cwd=workspace,
            timeout=5,
        )

    assert result.returncode == 0
    assert result.stdout == "ok"

    args = list(create_proc.call_args.args)
    assert args[0] == "/usr/bin/bwrap"
    assert "--unshare-all" in args
    assert "--share-net" not in args
    assert args[args.index("--bind") + 1] == str(workspace)
    assert args[args.index("--bind") + 2] == "/workspace"
    assert args[-3:] == ["/bin/bash", "-c", "echo ok"]


@pytest.mark.asyncio
async def test_sandboxed_shell_tool_reports_missing_bwrap(tmp_path):
    """Linux hosts without bwrap should get an actionable tool response."""
    with patch(
        "qwenpaw.agents.tools.sandboxed_shell.shutil.which",
        return_value=None,
    ), patch(
        "qwenpaw.agents.tools.sandboxed_shell.get_current_workspace_dir",
        return_value=str(tmp_path),
    ):
        response = await execute_sandboxed_shell_command("echo ok")

    block = response.content[0]
    text = block["text"] if isinstance(block, dict) else block.text
    assert "bubblewrap" in text.lower()
    assert "not found" in text.lower()
