"""Permit-bound client for Runtime-owned physical task sandboxes."""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Any, Mapping

import httpx

_ENDPOINT = "/runtime/internal/sandbox/execute"


class SandboxExecutorError(RuntimeError):
    """Physical execution was unavailable or returned an invalid envelope."""


@dataclass(frozen=True)
class RuntimeSandboxExecutor:
    base_url: str
    token: str
    sandbox_context: dict[str, Any]

    @classmethod
    def from_request(
        cls,
        *,
        base_url: str,
        sandbox_context: Any,
    ) -> "RuntimeSandboxExecutor | None":
        if not isinstance(sandbox_context, Mapping):
            return None
        token = str(os.environ.get("QWENPAW_SERVICE_TOKEN") or "").strip()
        if not token:
            return None
        return cls(
            base_url=str(base_url).rstrip("/"),
            token=token,
            sandbox_context=dict(sandbox_context),
        )

    async def execute(
        self,
        *,
        tool_call_id: str,
        tool_name: str,
        tool_input: Mapping[str, Any],
    ) -> dict[str, Any]:
        operation = _operation_for(tool_name)
        if not operation:
            raise SandboxExecutorError("Tool has no Runtime sandbox operation")
        payload = {
            "sandbox_context": dict(self.sandbox_context),
            "tool_call_id": str(tool_call_id),
            "tool_name": str(tool_name),
            "operation": operation,
            "arguments": dict(tool_input),
        }
        try:
            async with httpx.AsyncClient(
                timeout=65,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                response = await client.post(
                    f"{self.base_url}{_ENDPOINT}",
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {self.token}",
                        "Content-Type": "application/json",
                    },
                )
        except httpx.HTTPError as exc:
            raise SandboxExecutorError("Runtime sandbox operation failed") from exc
        if response.status_code >= 400:
            raise SandboxExecutorError("Runtime sandbox operation rejected")
        try:
            envelope = response.json()
        except ValueError as exc:
            raise SandboxExecutorError("Runtime sandbox response is invalid") from exc
        data = envelope.get("data") if isinstance(envelope, dict) else None
        if not isinstance(data, dict):
            raise SandboxExecutorError("Runtime sandbox response is invalid")
        return data


def is_physical_tool(tool_name: str) -> bool:
    return bool(_operation_for(tool_name))


def _operation_for(tool_name: str) -> str:
    return {
        "execute_shell_command": "shell.exec",
        "shell.exec": "shell.exec",
        "read_file": "file.read",
        "write_file": "file.write",
        "edit_file": "file.edit",
        "append_file": "file.append",
        "glob_search": "file.glob",
        "grep_search": "file.grep",
        "browser_use": "browser.execute",
    }.get(str(tool_name), "")


__all__ = [
    "RuntimeSandboxExecutor",
    "SandboxExecutorError",
    "is_physical_tool",
]
