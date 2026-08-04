# -*- coding: utf-8 -*-
"""Permit-bound client for Runtime-owned per-task execution sandboxes."""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any
from pathlib import Path
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ..config.context import (
    get_current_runtime_sandbox_context,
    get_current_runtime_tool_execution,
    get_current_runtime_tool_gateway,
)


class SandboxExecutorClientError(RuntimeError):
    pass


def _runtime_service_token() -> str:
    """Return the service credential accepted by Runtime internal APIs."""
    return str(
        os.environ.get("QWENPAW_SERVICE_TOKEN")
        or os.environ.get("RUNTIME_QWENPAW_SERVICE_TOKEN")
        or "",
    ).strip()


@dataclass(frozen=True)
class RuntimeSandboxExecutorClient:
    base_url: str
    token: str
    sandbox_context: dict[str, Any]
    tool_call_id: str
    tool_name: str
    tool_input: dict[str, Any]

    @classmethod
    def from_current_context(cls) -> "RuntimeSandboxExecutorClient | None":
        sandbox_context = get_current_runtime_sandbox_context()
        gateway = get_current_runtime_tool_gateway()
        execution = get_current_runtime_tool_execution()
        if sandbox_context is None and gateway is None and execution is None:
            return None
        if (
            not isinstance(sandbox_context, dict)
            or not isinstance(gateway, dict)
            or not isinstance(execution, dict)
        ):
            raise SandboxExecutorClientError("Runtime sandbox execution context is incomplete")
        base_url = str(gateway.get("base_url", "")).strip().rstrip("/")
        token = _runtime_service_token()
        tool_call_id = str(execution.get("tool_call_id", "")).strip()
        tool_name = str(execution.get("tool_name", "")).strip()
        tool_input = execution.get("tool_input", {})
        if not token:
            raise SandboxExecutorClientError("Runtime service token is unavailable")
        if not base_url or not tool_call_id or not tool_name or not isinstance(tool_input, dict):
            raise SandboxExecutorClientError("Runtime sandbox execution context is incomplete")
        return cls(
            base_url=base_url,
            token=token,
            sandbox_context=dict(sandbox_context),
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            tool_input=dict(tool_input),
        )

    async def execute(self, operation: str) -> dict[str, Any]:
        payload = {
            "sandbox_context": dict(self.sandbox_context),
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "operation": str(operation),
            "arguments": dict(self.tool_input),
        }
        response = await asyncio.to_thread(self._post, payload)
        data = response.get("data") if isinstance(response, dict) else None
        if not isinstance(data, dict):
            raise SandboxExecutorClientError("Runtime sandbox returned an invalid response")
        return data

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = Request(
            f"{self.base_url}/runtime/internal/sandbox/execute",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=65) as response:  # noqa: S310 - fixed Runtime URL
                body = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise SandboxExecutorClientError("Runtime sandbox operation failed") from exc
        if not isinstance(body, dict):
            raise SandboxExecutorClientError("Runtime sandbox returned an invalid response")
        return body


@dataclass(frozen=True)
class RuntimeSandboxAttachmentProcessorClient:
    base_url: str
    token: str
    sandbox_context: dict[str, Any]

    @classmethod
    def from_current_context(cls) -> "RuntimeSandboxAttachmentProcessorClient | None":
        sandbox_context = get_current_runtime_sandbox_context()
        gateway = get_current_runtime_tool_gateway()
        if not isinstance(sandbox_context, dict) or sandbox_context.get("isolation_level") != "container":
            return None
        if not isinstance(gateway, dict):
            raise SandboxExecutorClientError("Runtime sandbox attachment context is incomplete")
        base_url = str(gateway.get("base_url", "")).strip().rstrip("/")
        token = _runtime_service_token()
        if not token:
            raise SandboxExecutorClientError("Runtime service token is unavailable")
        if not base_url:
            raise SandboxExecutorClientError("Runtime sandbox attachment context is incomplete")
        return cls(base_url=base_url, token=token, sandbox_context=dict(sandbox_context))

    def process(self, prepared_files: list[Any]) -> dict[str, Any]:
        task_id = str(self.sandbox_context.get("task_id", "")).strip()
        cache_root = Path(
            os.environ.get("QWENPAW_TASK_FILE_ROOT", "/tmp/qwenpaw-runtime-task-files"),
        ).expanduser().resolve() / task_id
        attachments: list[dict[str, Any]] = []
        for prepared in prepared_files:
            local_path = Path(prepared.local_path).resolve()
            try:
                relative_path = local_path.relative_to(cache_root).as_posix()
            except ValueError as exc:
                raise SandboxExecutorClientError("Runtime attachment is outside the task input root") from exc
            attachments.append(
                {
                    "file_id": str(prepared.file_id),
                    "relative_path": relative_path,
                    "content_type": str(prepared.content_type),
                    "size_bytes": int(prepared.size_bytes),
                    "original_name": str(prepared.original_name),
                    "expires_at": str(prepared.expires_at),
                },
            )
        request = Request(
            f"{self.base_url}/runtime/internal/sandbox/attachments/process",
            data=json.dumps(
                {"sandbox_context": self.sandbox_context, "attachments": attachments},
                ensure_ascii=False,
            ).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=65) as response:  # noqa: S310 - fixed Runtime URL
                body = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise SandboxExecutorClientError("Runtime sandbox attachment processing failed") from exc
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict):
            raise SandboxExecutorClientError("Runtime sandbox attachment processing returned invalid data")
        return data
