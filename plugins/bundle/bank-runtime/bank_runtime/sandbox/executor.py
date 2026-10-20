"""Permit-bound client for Runtime-owned physical task sandboxes."""

from __future__ import annotations

from dataclasses import dataclass
import os
import math
import asyncio
import time
import re
from datetime import datetime, timezone
from typing import Any, Mapping

import httpx

_ENDPOINT = "/runtime/internal/sandbox/execute"


class SandboxExecutorError(RuntimeError):
    """Physical execution was unavailable or returned an invalid envelope."""
    def __init__(self, message, code="DOCUMENT_ENGINE_UNAVAILABLE", *, argument_reason="", reason='', details=None):
        super().__init__(message)
        self.code = code
        self.error_code = code
        self.argument_reason = argument_reason
        self.reason = reason
        self.details = dict(details or {})


class SandboxExecutionUnknown(SandboxExecutorError):
    execution_uncertain = True

    def __init__(self):
        super().__init__('Native execution state is unknown; do not repeat this call',
            'TOOL_EXECUTION_UNKNOWN', reason='sandbox_job_state_unknown')


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
        if (self.sandbox_context.get('native_analysis_enabled') is True
                and operation in {'shell.exec','file.read','file.write','file.edit','file.append','file.glob','file.grep'}):
            if self.sandbox_context.get('isolation_level') != 'container':
                raise SandboxExecutorError('Native physical sandbox unavailable', 'FORBIDDEN')
            return await self._execute_native_job(payload)
        remaining = _remaining_seconds(self.sandbox_context)
        maximum = _command_timeout(self.sandbox_context.get("command_max_timeout_seconds"), 1800, 1800)
        default = _command_timeout(self.sandbox_context.get("command_default_timeout_seconds"), 300, maximum)
        requested = _command_timeout(tool_input.get("timeout"), default, maximum) if operation == "shell.exec" else default
        wait_seconds = min(remaining, requested + 5)
        try:
            async with asyncio.timeout(wait_seconds):
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(wait_seconds, connect=min(5, wait_seconds), write=min(30, wait_seconds), pool=min(5, wait_seconds)),
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
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise SandboxExecutorError("Sandbox execution deadline exceeded", "DOCUMENT_EXECUTION_TIMEOUT") from exc
        except httpx.HTTPError as exc:
            raise SandboxExecutorError("Runtime sandbox operation failed") from exc
        if response.status_code >= 400:
            raise _technical_rejection(response)
        try:
            envelope = response.json()
        except ValueError as exc:
            raise SandboxExecutorError("Runtime sandbox response is invalid") from exc
        data = envelope.get("data") if isinstance(envelope, dict) else None
        if not isinstance(data, dict):
            raise SandboxExecutorError("Runtime sandbox response is invalid")
        return data

    async def _execute_native_job(self, payload):
        from ..gateway.protocol import canonical_payload_hash
        remaining = _remaining_seconds(self.sandbox_context)
        deadline = time.monotonic() + remaining
        query = {key: value for key, value in payload.items() if key != 'arguments'}
        query['input_hash'] = canonical_payload_hash(payload['arguments'])
        submit = True
        attempts = 0
        poll_interval = .05
        try:
            async with asyncio.timeout(remaining):
                async with httpx.AsyncClient(follow_redirects=False, trust_env=False) as client:
                    while time.monotonic() < deadline:
                        budget = min(30, deadline-time.monotonic())
                        endpoint = '/runtime/internal/sandbox/jobs/' + ('submit' if submit else 'query')
                        if submit:
                            attempts += 1
                        try:
                            response = await client.post(self.base_url + endpoint,
                                json=payload if submit else query,
                                headers={'Authorization': f'Bearer {self.token}', 'Content-Type':'application/json'},
                                timeout=httpx.Timeout(budget, connect=min(5,budget), write=budget, pool=min(5,budget)))
                        except httpx.HTTPError:
                            # An accepted request may have lost only its HTTP receipt.
                            submit = False
                            await asyncio.sleep(min(poll_interval, max(0,deadline-time.monotonic())))
                            poll_interval = min(1, poll_interval*2)
                            continue
                        if response.status_code >= 400:
                            if response.status_code >= 500:
                                submit = False
                                await asyncio.sleep(min(poll_interval,max(0,deadline-time.monotonic())))
                                poll_interval = min(1,poll_interval*2)
                                continue
                            raise _technical_rejection(response)
                        try:
                            if len(response.content) > 16*1024**2 + 32768:
                                raise ValueError('Native result exceeds bound')
                            envelope = response.json()
                            data = envelope['data']
                            if not isinstance(data, dict):
                                raise ValueError('Invalid native result')
                        except (ValueError, KeyError, TypeError):
                            submit = False
                            await asyncio.sleep(min(poll_interval,max(0,deadline-time.monotonic())))
                            poll_interval = min(1,poll_interval*2)
                            continue
                        status = data.get('status')
                        if status in {'succeeded','failed','cancelled'}:
                            result = data.get('result')
                            if isinstance(result, dict):
                                return {**result, **({'execution_cancelled':True} if status == 'cancelled' else {})}
                            error = data.get('error') or {}
                            fake_response = httpx.Response(502, json={'detail':{'code':error.get('code','WORKER_FAILED'),
                                'details':{'reason':error.get('reason','sandbox_execution_failed'), **(error.get('details') or {})}}})
                            raise _technical_rejection(fake_response)
                        if status == 'unknown':
                            raise SandboxExecutionUnknown()
                        if status == 'not_found':
                            if attempts >= 3:
                                raise SandboxExecutionUnknown()
                            submit = True  # Same canonical call; SQL uniqueness remains authoritative.
                        elif status in {'queued','running'} or (status=='unavailable' and data.get('reason')=='sandbox_job_metadata_busy'):
                            submit = False
                        else:
                            raise SandboxExecutionUnknown()
                        await asyncio.sleep(min(poll_interval,max(0,deadline-time.monotonic())))
                        poll_interval = min(1,poll_interval*2)
        except TimeoutError as exc:
            raise SandboxExecutionUnknown() from exc
        raise SandboxExecutionUnknown()

    async def process_documents(self, *, tool_call_id, name, arguments, plan):
        tool_name = {"parse_documents": "document.parse", "read_document_chunks": "document.read_chunks",
                     "read_range": "document.read_range", "aggregate": "document.aggregate", "search": "document.search",
                     "analyze": "document.analyze"}.get(name)
        if not tool_name:
            raise SandboxExecutorError("Unregistered processing action")
        remaining = _remaining_seconds(self.sandbox_context)
        try:
            async with asyncio.timeout(remaining):
                async with httpx.AsyncClient(timeout=httpx.Timeout(remaining, connect=min(5, remaining), write=min(30, remaining), pool=min(5, remaining)), trust_env=False, follow_redirects=False) as client:
                    response = await client.post(f"{self.base_url}{_ENDPOINT}",
                        headers={"Authorization": f"Bearer {self.token}"},
                        json={"sandbox_context": self.sandbox_context, "tool_call_id": tool_call_id,
                              "tool_name": tool_name, "operation": tool_name, "arguments": arguments, "processing": plan})
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise SandboxExecutorError("Processing deadline exceeded", "DOCUMENT_EXECUTION_TIMEOUT") from exc
        except httpx.HTTPError as exc:
            raise SandboxExecutorError("Processing service unavailable") from exc
        if response.status_code >= 400:
            try:
                details = response.json().get("detail", {}).get("details", {})
            except ValueError:
                details = {}
            raise SandboxExecutorError("File processing rejected", details.get("processing_error", "DOCUMENT_ENGINE_UNAVAILABLE"),
                                       argument_reason=details.get('argument_reason', ''))
        try:
            data = response.json().get("data")
        except (ValueError, AttributeError) as exc:
            raise SandboxExecutorError("Invalid processing response") from exc
        if not isinstance(data, dict):
            raise SandboxExecutorError("Invalid processing response")
        return data

    async def validate_sources(self, sources):
        """Recheck current grants without consuming or extending them."""
        wait_seconds = min(30, _remaining_seconds(self.sandbox_context))
        try:
            async with asyncio.timeout(wait_seconds):
                async with httpx.AsyncClient(timeout=httpx.Timeout(wait_seconds, connect=min(5, wait_seconds), write=wait_seconds, pool=min(5, wait_seconds)), trust_env=False, follow_redirects=False) as client:
                    response = await client.post(f"{self.base_url}/runtime/internal/sandbox/attachments/validate-materialized",
                        headers={"Authorization": f"Bearer {self.token}"},
                        json={"sandbox_context": self.sandbox_context, "file_ids": [item["file_id"] for item in sources]})
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise SandboxExecutorError("Source validation deadline exceeded", "DOCUMENT_EXECUTION_TIMEOUT") from exc
        if response.status_code >= 400:
            raise SandboxExecutorError("Source authorization rejected", "FILE_ACCESS_DENIED")
        records = {item["file_id"]: item for item in response.json().get("data", {}).get("files", [])}
        if len(records) != len(sources) or any(records.get(item["file_id"], {}).get("content_hash") != item["sha256"] for item in sources):
            raise SandboxExecutorError("Source integrity changed", "FILE_REF_INVALID")


@dataclass(frozen=True)
class AuthorizedReadingExecution:
    executor: RuntimeSandboxExecutor
    tool_call_id: str
    name: str
    arguments: dict[str, Any]

    async def execute(self, plan):
        return await self.executor.process_documents(tool_call_id=self.tool_call_id, name=self.name,
                                                       arguments=self.arguments, plan=plan)

    async def validate_sources(self, sources):
        return await self.executor.validate_sources(sources)


def _remaining_seconds(context: Mapping[str, Any]) -> float:
    expiry_value = context.get("expires_at")
    try:
        if not isinstance(expiry_value, str):
            raise ValueError("invalid expiry")
        expiry = datetime.fromisoformat(expiry_value.replace("Z", "+00:00"))
        if expiry.tzinfo is None:
            raise ValueError("invalid expiry")
        remaining = (expiry - datetime.now(timezone.utc)).total_seconds()
    except (TypeError, ValueError, OverflowError) as exc:
        raise SandboxExecutorError("Invalid task execution deadline", "DOCUMENT_REF_EXPIRED") from exc
    if not math.isfinite(remaining) or remaining <= 0:
        raise SandboxExecutorError("Task execution expired", "DOCUMENT_REF_EXPIRED")
    return min(remaining, 3600)


def bind_native_job_deadline(context: Mapping[str, Any]) -> None:
    """Keep the model waiting for a permit-bound job, including its queue wait.

    Runtime owns the command deadline. The outer coordinator must instead use
    the task expiry, without extending an already earlier cancellation deadline.
    """
    if (context.get('native_analysis_enabled') is not True
            or context.get('isolation_level') != 'container'):
        return
    from qwenpaw.tool_calls import get_call_context

    remaining = _remaining_seconds(context)
    call = get_call_context()
    if call is None:
        return
    deadline = asyncio.get_running_loop().time() + remaining
    call.kill_deadline = min(call.kill_deadline, deadline) if call.kill_deadline is not None else deadline
    # Detached tool hints cannot represent the actual result of a Runtime job.
    call.offload_deadline = None
    call.deadline_changed_event.set()


def _command_timeout(value: Any, default: int, maximum: int) -> int:
    try:
        if isinstance(value, bool):
            raise ValueError("invalid timeout")
        seconds = float(value)
        if not math.isfinite(seconds):
            raise ValueError("invalid timeout")
        return min(maximum, max(1, int(seconds)))
    except (TypeError, ValueError, OverflowError):
        return min(default, maximum)


def is_physical_tool(tool_name: str) -> bool:
    return bool(_operation_for(tool_name))


def _technical_rejection(response):
    # Stable codes/reasons are protocol data. Never expose server messages,
    # tracebacks, locators, tokens, or arbitrary error detail values.
    try:
        envelope = response.json()
    except ValueError:
        envelope = {}
    detail = envelope.get('detail', {}) if isinstance(envelope, dict) else {}
    if not isinstance(detail, dict):detail = {}
    code = detail.get('code')
    if not isinstance(code,str) or code not in {'WORKER_TIMEOUT','WORKER_UNAVAILABLE','WORKER_FAILED','FORBIDDEN','UNAUTHORIZED',
                    'FILE_ACCESS_DENIED','BAD_REQUEST','INVALID_REQUEST'}:
        code = 'DOCUMENT_ENGINE_UNAVAILABLE'
    raw = detail.get('details', {})
    if not isinstance(raw, dict):raw = {}
    reason = raw.get('reason')
    if not isinstance(reason,str) or reason not in {'sandbox_command_timeout','sandbox_container_oom','sandbox_container_dead',
                      'sandbox_execution_failed','sandbox_executor_failed','sandbox_output_limit',
                      'sandbox_rpc_frame_invalid','sandbox_rpc_frame_too_large','sandbox_rpc_input_too_large',
                      'sandbox_command_cancelled','sandbox_output_consumer_failed','sandbox_background_pipes','sandbox_transport_failed',
                      'sandbox_process_cleanup_incomplete',
                      'sandbox_log_io_failed','sandbox_log_metadata_failed','sandbox_log_limit','sandbox_log_incomplete',
                      'sandbox_log_stopped','sandbox_log_interrupted','sandbox_log_capacity_unavailable','sandbox_log_authority_denied',
                      'sandbox_private_state_lost','path_escape','symlink_rejected','hardlink_rejected',
                      'write_scope_rejected','sandbox_scope_inactive','sandbox_context_expired',
                        'sandbox_task_stopped',
                        'sandbox_task_deadline_exhausted','sandbox_task_authority_revoked','sandbox_resource_wait_timeout',
                        'sandbox_trusted_authority_missing',
                      'sandbox_execution_replay','original_authority_changed','original_unavailable',
                      'binary_file_requires_parser'}:
        reason = ''
    safe = {key: raw[key] for key in ('timed_out','container_available','oom_killed','state_lost','task_deadline_exhausted','authority_revoked',
            'stdout_truncated','stderr_truncated')
            if type(raw.get(key)) is bool}
    safe.update(safe_native_output_metadata(raw))
    if reason:safe['reason'] = reason
    if raw.get('notice') == 'sandbox_private_state_lost':safe['notice'] = raw['notice']
    error = SandboxExecutorError('Runtime sandbox operation rejected: ' + code + (': ' + reason if reason else '')
                                + ('; sandbox_private_state_lost' if safe.get('state_lost') is True else ''),
                                code, reason=reason, details=safe)
    if reason == 'binary_file_requires_parser':
        error.validation_hint = ('binary_file_requires_parser: read_file reads text only. '
            'Read this authorized original file with an appropriate installed Python format reader '
            'through execute_shell_command. No conversion or new authorization is required by this error.')
    return error


def safe_native_output_metadata(result, *, stdout='', stderr=''):
    """Finite public transport facts only; arbitrary paths/reasons never pass."""
    safe = {}
    for key,text in (('stdout_bytes',stdout),('stderr_bytes',stderr)):
        count = result.get(key)
        safe[key] = count if type(count) is int and 0 <= count <= 2*1024**3+8192 else len(text.encode('utf-8'))
    reason = result.get('technical_reason')
    safe['technical_reason'] = reason if isinstance(reason,str) and reason in {
        'sandbox_output_limit','sandbox_command_timeout','sandbox_command_cancelled',
        'sandbox_output_consumer_failed','sandbox_background_pipes','sandbox_transport_failed','sandbox_rpc_frame_invalid',
        'sandbox_process_cleanup_incomplete',
        'sandbox_log_io_failed','sandbox_log_metadata_failed','sandbox_log_limit','sandbox_log_incomplete',
        'sandbox_log_stopped','sandbox_log_interrupted','sandbox_log_capacity_unavailable','sandbox_log_authority_denied',
        'sandbox_rpc_frame_too_large','sandbox_rpc_input_too_large','sandbox_container_dead','sandbox_container_oom'} else ''
    safe['excerpt_mode'] = 'head_tail' if result.get('excerpt_mode') == 'head_tail' else 'complete'
    stdout_path,stderr_path=result.get('stdout_log_path'),result.get('stderr_log_path')
    match=re.fullmatch(r'/workspace/public/execution_logs/([A-Za-z0-9_-]{1,160})/stdout\.log',stdout_path) if isinstance(stdout_path,str) else None
    log_reason=result.get('log_interruption')
    if (match and stderr_path=='/workspace/public/execution_logs/'+match.group(1)+'/stderr.log'
            and all(type(result.get(key)) is int and 0<=result[key]<=1024**3
                    for key in ('stdout_log_bytes','stderr_log_bytes'))
            and type(result.get('logs_complete')) is bool and isinstance(log_reason,str)
            and log_reason in {'','sandbox_output_limit','sandbox_command_timeout','sandbox_command_cancelled',
                'sandbox_output_consumer_failed','sandbox_background_pipes','sandbox_transport_failed',
                'sandbox_process_cleanup_incomplete','sandbox_container_dead','sandbox_container_oom',
                'sandbox_task_deadline_exhausted','sandbox_task_authority_revoked','sandbox_log_io_failed',
                'sandbox_log_metadata_failed','sandbox_log_limit','sandbox_log_incomplete',
                'sandbox_log_stopped','sandbox_log_interrupted'}):
        safe.update({key:result[key] for key in ('stdout_log_path','stderr_log_path','stdout_log_bytes',
            'stderr_log_bytes','logs_complete','log_interruption')})
    return safe


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
        "browser": "browser.execute",
    }.get(str(tool_name), "")


__all__ = [
    "RuntimeSandboxExecutor",
    "SandboxExecutorError",
    "is_physical_tool",
]
