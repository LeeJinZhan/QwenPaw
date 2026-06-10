# -*- coding: utf-8 -*-
"""Sandboxed shell command tool backed by Linux bubblewrap."""
from __future__ import annotations

import asyncio
import os
import shutil
import signal
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from agentscope.message import TextBlock
from agentscope.tool import ToolResponse

from ...config.config import SandboxProfileConfig
from ...config.context import (
    get_current_sandbox_profile,
    get_current_shell_command_timeout,
    get_current_workspace_dir,
)
from ...constant import WORKING_DIR
from .shell import _collapse_embedded_newlines, smart_decode

SANDBOX_WORKSPACE = "/workspace"
SANDBOX_HOME = "/home/sandbox"


@dataclass
class ShellExecutionResult:
    """Normalized shell execution result."""

    returncode: int
    stdout: str
    stderr: str


class SandboxConfigurationError(RuntimeError):
    """Raised when sandbox execution cannot be configured safely."""


def _default_profile_for_workspace(workspace_dir: Path) -> SandboxProfileConfig:
    """Return a conservative default when no agent profile is in context."""
    return SandboxProfileConfig(
        enabled=True,
        profile_id="linux-bubblewrap-workspace",
        engine="bubblewrap",
        allow_network=False,
        writable_roots=[str(workspace_dir)],
        home_dir=str(workspace_dir / ".qwenpaw-sandbox" / "home"),
        shell_executable="/bin/sh",
    )


def _coerce_profile(profile: object, workspace_dir: Path) -> SandboxProfileConfig:
    """Coerce context/dict profile values into SandboxProfileConfig."""
    if profile is None:
        return _default_profile_for_workspace(workspace_dir)
    if isinstance(profile, SandboxProfileConfig):
        return profile
    if isinstance(profile, dict):
        return SandboxProfileConfig.model_validate(profile)
    return SandboxProfileConfig.model_validate(
        getattr(profile, "model_dump", lambda: profile)(),
    )


def _format_response(returncode: int, stdout: str, stderr: str) -> ToolResponse:
    """Format execution output consistently with the legacy shell tool."""
    if returncode == 0:
        response_text = stdout or "Command executed successfully (no output)."
        if stderr:
            response_text += f"\n[stderr]\n{stderr}"
    else:
        parts = [f"Command failed with exit code {returncode}."]
        if stdout:
            parts.append(f"\n[stdout]\n{stdout}")
        if stderr:
            parts.append(f"\n[stderr]\n{stderr}")
        response_text = "".join(parts)

    return ToolResponse(
        content=[
            TextBlock(
                type="text",
                text=response_text,
            ),
        ],
    )


def _resolve_inside_mount(host_path: Path, host_root: Path) -> str:
    """Map a host path under host_root to its sandbox /workspace path."""
    try:
        relative = host_path.resolve(strict=False).relative_to(
            host_root.resolve(strict=False),
        )
    except ValueError as exc:
        raise SandboxConfigurationError(
            f"cwd must be inside sandbox writable root: {host_root}",
        ) from exc

    if str(relative) == ".":
        return SANDBOX_WORKSPACE
    return str(Path(SANDBOX_WORKSPACE) / relative)


class BubblewrapShellExecutor:
    """Execute shell commands inside a bubblewrap mount/network namespace."""

    def __init__(self, profile: SandboxProfileConfig) -> None:
        self.profile = profile

    def _build_args(
        self,
        *,
        command: str,
        cwd: Path,
        env: dict[str, str],
    ) -> list[str]:
        bwrap = shutil.which("bwrap")
        if not bwrap:
            raise SandboxConfigurationError(
                "bubblewrap executable not found. Install 'bubblewrap' "
                "on the Linux host before using this tool.",
            )

        writable_roots = [p for p in self.profile.writable_roots if p]
        if not writable_roots:
            raise SandboxConfigurationError(
                "sandbox profile must define at least one writable root",
            )

        host_workspace = Path(writable_roots[0]).expanduser()
        sandbox_cwd = _resolve_inside_mount(cwd.expanduser(), host_workspace)

        home_dir = Path(
            self.profile.home_dir
            or str(host_workspace / ".qwenpaw-sandbox" / "home"),
        ).expanduser()
        home_dir.mkdir(parents=True, exist_ok=True)

        shell_executable = self.profile.shell_executable or "/bin/sh"

        args = [
            bwrap,
            "--die-with-parent",
            "--unshare-all",
            "--new-session",
            "--clearenv",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",
        ]
        if self.profile.allow_network:
            args.append("--share-net")

        for path in ("/usr", "/bin", "/lib", "/lib64", "/etc"):
            if Path(path).exists():
                args.extend(["--ro-bind", path, path])

        for path in self.profile.readonly_roots:
            if path:
                host_path = str(Path(path).expanduser())
                args.extend(["--ro-bind", host_path, host_path])

        args.extend(["--bind", str(host_workspace), SANDBOX_WORKSPACE])
        args.extend(["--bind", str(home_dir), SANDBOX_HOME])
        args.extend(["--chdir", sandbox_cwd])
        args.extend(["--setenv", "HOME", SANDBOX_HOME])
        args.extend(["--setenv", "PATH", env.get("PATH", "")])
        args.extend([shell_executable, "-c", command])
        return args

    async def execute(
        self,
        command: str,
        *,
        cwd: Path,
        timeout: float,
    ) -> ShellExecutionResult:
        env = os.environ.copy()
        python_bin_dir = str(Path(sys.executable).parent)
        existing_path = env.get("PATH", "")
        env["PATH"] = (
            python_bin_dir + os.pathsep + existing_path
            if existing_path
            else python_bin_dir
        )

        args = self._build_args(command=command, cwd=cwd, env=env)
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            start_new_session=True,
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout,
            )
            return ShellExecutionResult(
                returncode=proc.returncode if proc.returncode is not None else -1,
                stdout=smart_decode(stdout),
                stderr=smart_decode(stderr),
            )
        except asyncio.TimeoutError:
            timeout_msg = (
                f"Command execution exceeded the timeout of {timeout} seconds."
            )
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2)
                except asyncio.TimeoutError:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    await asyncio.wait_for(proc.wait(), timeout=2)
            except (ProcessLookupError, OSError):
                try:
                    proc.kill()
                    await proc.wait()
                except (ProcessLookupError, OSError):
                    pass

            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=1,
                )
            except asyncio.TimeoutError:
                stdout, stderr = b"", b""
            stderr_text = smart_decode(stderr)
            if stderr_text:
                stderr_text = f"{stderr_text}\n{timeout_msg}"
            else:
                stderr_text = timeout_msg
            return ShellExecutionResult(
                returncode=-1,
                stdout=smart_decode(stdout),
                stderr=stderr_text,
            )


async def execute_sandboxed_shell_command(
    command: str,
    timeout: float = 60.0,
    cwd: Optional[Path] = None,
) -> ToolResponse:
    """Execute a shell command inside the configured Linux sandbox."""
    cmd = _collapse_embedded_newlines((command or "").strip())

    if isinstance(timeout, str):
        try:
            timeout = float(timeout)
        except (ValueError, TypeError):
            timeout = 60.0
    if timeout == 60.0:
        configured = get_current_shell_command_timeout()
        if configured is not None:
            timeout = configured

    workspace_dir = Path(get_current_workspace_dir() or WORKING_DIR)
    if cwd is not None:
        cwd_path = Path(cwd)
        working_dir = cwd_path if cwd_path.is_absolute() else workspace_dir / cwd_path
    else:
        working_dir = workspace_dir

    try:
        profile = _coerce_profile(get_current_sandbox_profile(), workspace_dir)
        if not profile.enabled or profile.engine != "bubblewrap":
            raise SandboxConfigurationError(
                "No bubblewrap sandbox profile is configured for this agent.",
            )
        result = await BubblewrapShellExecutor(profile).execute(
            cmd,
            cwd=working_dir,
            timeout=timeout,
        )
        return _format_response(result.returncode, result.stdout, result.stderr)
    except Exception as exc:
        return ToolResponse(
            content=[
                TextBlock(
                    type="text",
                    text=f"Error: sandboxed shell command execution failed: {exc}",
                ),
            ],
        )


__all__ = [
    "BubblewrapShellExecutor",
    "SandboxProfileConfig",
    "execute_sandboxed_shell_command",
]
