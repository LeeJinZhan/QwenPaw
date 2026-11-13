"""Runtime-owned persistence for authenticated, fenced bank executions.

This protocol stages candidates; only Runtime can commit a conversation head.
No operation falls back to local files when remote storage is unavailable.
"""
from __future__ import annotations

import copy
import asyncio
from dataclasses import dataclass
import hashlib
import json
import re
import threading
from typing import Any
from urllib.parse import urlsplit

import httpx

from qwenpaw.exceptions import AgentRuntimeErrorException

PROTOCOL = "bank-stateless-v1"
ENDPOINT = "/runtime/v1/worker-state"
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,191}\Z")
_RESULT = re.compile(r"runtime-result:result_[a-f0-9]{64}\Z")
EMPTY_INPUT_HASH = hashlib.sha256(b'').hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class RemoteStateError(AgentRuntimeErrorException):
    def __init__(self, code: str = "WORKER_STATE_UNAVAILABLE", *, http_status: int | None = None, reason: str = '') -> None:
        safe = code if code in {"WORKER_EPOCH_STALE", "WORKER_STATE_INVALID", "WORKER_STATE_UNAVAILABLE",
            "WORKER_STATE_CONFLICT", "WORKER_SCOPE_MISMATCH", "WORKER_RESULT_NOT_FOUND",
            "WORKER_CONTEXT_BUDGET_UNAVAILABLE", "WORKER_CONTEXT_BUDGET_EXCEEDED", "WORKER_POLICY_MISMATCH"} else "WORKER_STATE_UNAVAILABLE"
        super().__init__(error_code=safe, message=safe, details={})
        self.http_status = http_status
        self.reason = reason if reason == 'input_revision_changed' else ''


def _identifier(value: Any) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise RemoteStateError("WORKER_STATE_INVALID")
    return value


def _integer(value: Any, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= 2**63 - 1:
        raise RemoteStateError("WORKER_STATE_INVALID")
    return value


@dataclass(frozen=True)
class RemoteExecution:
    base_url: str
    token: str
    task_id: str
    session_id: str
    user_id: str
    agent_id: str
    execution_id: str
    epoch: int
    branch_id: str
    base_revision: int
    policy_snapshot_id: str
    tool_session_id: str
    worker_id: str
    context_package: dict[str, Any]
    resume: bool = False
    lease_budget_seconds: int = 30
    input_revision: int = 0
    input_hash: str = EMPTY_INPUT_HASH
    progress_checkpoints: bool = False
    recovery_generation: str = ''
    model_capacity_enabled: bool = False

    def scope_payload(self) -> dict[str, Any]:
        return {"protocol": PROTOCOL, "task_id": self.task_id, "session_id": self.session_id,
            "user_id": self.user_id, "agent_id": self.agent_id, "worker_agent_id": self.agent_id,
            "execution_id": self.execution_id, "epoch": self.epoch, "branch_id": self.branch_id,
            "base_revision": self.base_revision, "policy_snapshot_id": self.policy_snapshot_id,
            "tool_session_id": self.tool_session_id, **({"worker_id": self.worker_id} if self.worker_id else {}),
            **({'recovery_generation':self.recovery_generation} if self.recovery_generation else {})}

    @property
    def run_key(self) -> str:
        return execution_run_key(self.agent_id, self.task_id, self.session_id, self.user_id,
            {"protocol": PROTOCOL, "execution_id": self.execution_id, "epoch": self.epoch, "branch_id": self.branch_id,
             'recovery_generation':self.recovery_generation})


def execution_run_key(agent_id: str, task_id: str, session_id: str, user_id: str, execution: Any) -> str:
    if not isinstance(execution, dict) or execution.get("protocol") != PROTOCOL:
        raise RemoteStateError("WORKER_STATE_INVALID")
    return "bank-execution:" + canonical_hash({"agent_id": _identifier(agent_id),
        "task_id": _identifier(task_id), "session_id": _identifier(session_id), "user_id": _identifier(user_id),
        "execution_id": _identifier(execution.get("execution_id")), "epoch": _integer(execution.get("epoch"), 1),
        "branch_id": _identifier(execution.get("branch_id")),
        **({'recovery_generation':execution['recovery_generation']} if execution.get('recovery_generation') else {})})


def execution_from_request(request: Any, agent_id: str) -> RemoteExecution | None:
    if getattr(request, "channel", "") != "bank-runtime":
        return None
    raw = getattr(request, "runtime_execution", None)
    if raw is None:
        return None
    if not isinstance(raw, dict) or raw.get("protocol") != PROTOCOL:
        raise RemoteStateError("WORKER_STATE_INVALID")
    gateway = getattr(request, "runtime_tool_gateway", None)
    if not isinstance(gateway, dict):
        raise RemoteStateError("WORKER_SCOPE_MISMATCH")
    task_id = _identifier(getattr(request, "runtime_task_id", None))
    session_id = _identifier(getattr(request, "session_id", None))
    user_id = _identifier(getattr(request, "user_id", None))
    agent_id = _identifier(agent_id)
    if (gateway.get("task_id") != task_id or gateway.get("session_id") != session_id
            or (gateway.get("worker_agent_id") or gateway.get("agent_id")) != agent_id
            or (gateway.get("agent_id") and gateway["agent_id"] != agent_id)):
        raise RemoteStateError("WORKER_SCOPE_MISMATCH")
    base_url = str(gateway.get("base_url") or "").rstrip("/")
    try:
        parsed = urlsplit(base_url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}):
            raise ValueError()
    except ValueError as exc:
        raise RemoteStateError("WORKER_STATE_INVALID") from exc
    token = gateway.get("token")
    if not isinstance(token, str) or not token.strip() or "\n" in token or "\r" in token:
        raise RemoteStateError("WORKER_SCOPE_MISMATCH")
    package = raw.get("context_package")
    if not isinstance(package, dict) or package.get("format") not in {"messages-v1", "agent-state-v1"}:
        raise RemoteStateError("WORKER_STATE_INVALID")
    base_revision = _integer(raw.get("base_revision"))
    if _integer(package.get("revision")) != base_revision:
        raise RemoteStateError("WORKER_STATE_CONFLICT")
    try:
        package_hash = canonical_hash({key: value for key, value in package.items() if key != "hash"})
    except (TypeError, ValueError, OverflowError) as exc:
        raise RemoteStateError("WORKER_STATE_INVALID") from exc
    if package.get("hash") != package_hash:
        raise RemoteStateError("WORKER_STATE_INVALID")
    if package["format"] == "agent-state-v1" and not isinstance(package.get("agent_state"), dict):
        raise RemoteStateError("WORKER_STATE_INVALID")
    if package["format"] == "messages-v1" and not isinstance(package.get("messages"), list):
        raise RemoteStateError("WORKER_STATE_INVALID")
    if type(raw.get("resume", False)) is not bool or (raw.get("resume") and package["format"] != "agent-state-v1"):
        raise RemoteStateError("WORKER_STATE_INVALID")
    confirmed_ids = package.get('confirmed_message_ids')
    if (confirmed_ids is not None and (not isinstance(confirmed_ids, list) or len(confirmed_ids) > 100000
            or any(not isinstance(item, str) or not 1 <= len(item) <= 256 for item in confirmed_ids)
            or len(set(confirmed_ids)) != len(confirmed_ids))) or (raw.get('resume') and confirmed_ids is None):
        raise RemoteStateError('WORKER_STATE_INVALID')
    budget = _integer(raw.get("lease_budget_seconds", 30), 5)
    if budget > 300:
        raise RemoteStateError("WORKER_STATE_INVALID")
    input_revision = _integer(raw.get('input_revision', 0))
    input_hash = raw.get('input_hash', EMPTY_INPUT_HASH)
    if (not isinstance(input_hash, str) or not re.fullmatch(r'[a-f0-9]{64}', input_hash)
            or input_revision == 0 and input_hash != EMPTY_INPUT_HASH
            or raw.get('resume') and (input_revision != package.get('input_revision', 0)
                                     or input_hash != package.get('input_hash', EMPTY_INPUT_HASH))):
        raise RemoteStateError('WORKER_STATE_INVALID')
    if type(raw.get('progress_checkpoints', False)) is not bool:
        raise RemoteStateError('WORKER_STATE_INVALID')
    generation = raw.get('recovery_generation','')
    if not isinstance(generation,str) or generation and not re.fullmatch('[a-f0-9]{32}',generation):
        raise RemoteStateError('WORKER_STATE_INVALID')
    if type(raw.get('model_capacity_enabled',False)) is not bool:
        raise RemoteStateError('WORKER_STATE_INVALID')
    return RemoteExecution(base_url, token, task_id, session_id, user_id, agent_id,
        _identifier(raw.get("execution_id")), _integer(raw.get("epoch"), 1),
        _identifier(raw.get("branch_id")), base_revision, _identifier(gateway.get("policy_snapshot_id")),
        _identifier(gateway.get("tool_session_id")), str(gateway.get("worker_id") or ""), copy.deepcopy(package),
        raw.get("resume", False), budget, input_revision, input_hash, raw.get('progress_checkpoints', False),generation,
        raw.get('model_capacity_enabled', False) is True)


class RemoteStateClient:
    endpoint = ENDPOINT

    def __init__(self, execution: RemoteExecution) -> None:
        self.execution = execution
        self.actual_policy_hash = ''
        self.input_revision = execution.input_revision
        self.input_hash = execution.input_hash
        self._pending_writes: set[str] = set()
        self._pending_lock = threading.Lock()

    def payload(self, operation: str, **values: Any) -> dict[str, Any]:
        return {**self.execution.scope_payload(), "operation": operation, **values}

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.execution.token}", "X-Agent-Id": self.execution.agent_id}

    @staticmethod
    def _response(response: httpx.Response) -> dict[str, Any]:
        try:
            body = response.json()
        except ValueError as exc:
            raise RemoteStateError() from exc
        if not response.is_success:
            detail = body.get("detail", body) if isinstance(body, dict) else {}
            code = detail.get("code") or detail.get("error_code") if isinstance(detail, dict) else ""
            details = detail.get('details', {}) if isinstance(detail, dict) else {}
            raise RemoteStateError(code, http_status=response.status_code,
                reason=details.get('reason', '') if isinstance(details, dict) else '')
        if not isinstance(body, dict):
            raise RemoteStateError("WORKER_STATE_INVALID")
        return body

    async def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=20, trust_env=False, follow_redirects=False) as client:
                response = await client.post(self.execution.base_url + self.endpoint, json=body, headers=self.headers)
            return self._response(response)
        except httpx.HTTPError as exc:
            raise RemoteStateError() from exc

    async def acquire_model(self, request_id, timeout_seconds):
        return await self._post(self.payload('model_acquire',request_id=request_id,timeout_seconds=timeout_seconds))

    async def release_model(self, request_id, outcome):
        return await self._post(self.payload('model_release',request_id=request_id,outcome=outcome))

    def _post_sync(self, body: dict[str, Any]) -> dict[str, Any]:
        # Called only inside the existing pruning worker thread.
        try:
            with httpx.Client(timeout=20, trust_env=False, follow_redirects=False) as client:
                response = client.post(self.execution.base_url + self.endpoint, json=body, headers=self.headers)
            return self._response(response)
        except httpx.HTTPError as exc:
            raise RemoteStateError() from exc

    async def stage(self, state: dict[str, Any]) -> str:
        with self._pending_lock:
            if self._pending_writes:
                raise RemoteStateError("WORKER_STATE_UNAVAILABLE")
        state_hash = canonical_hash(state)
        key = f"{self.execution.execution_id}\0{self.execution.epoch}\0{state_hash}"
        candidate_id = "candidate_" + hashlib.sha256(key.encode()).hexdigest()
        result = await self._post(self.payload("stage", agent_state=state, state_hash=state_hash, candidate_id=candidate_id,
            actual_policy_hash=self.actual_policy_hash, input_revision=self.input_revision, input_hash=self.input_hash))
        if result.get("status") != "staged" or result.get("candidate_id") != candidate_id or result.get("state_hash") != state_hash:
            raise RemoteStateError("WORKER_STATE_CONFLICT")
        return candidate_id

    async def checkpoint(self, state: dict[str, Any], child_task_ids: list[str], *, waiting_inputs: bool = False, progress: bool = False) -> str:
        with self._pending_lock:
            if self._pending_writes:
                raise RemoteStateError("WORKER_STATE_UNAVAILABLE")
        if (not child_task_ids and not (waiting_inputs or progress)) or len(child_task_ids) > 16 or (waiting_inputs or progress) and child_task_ids or waiting_inputs and progress:
            raise RemoteStateError("WORKER_STATE_INVALID")
        children = sorted({_identifier(item) for item in child_task_ids})
        state_hash = canonical_hash(state)
        key = f"{self.execution.execution_id}\0{self.execution.epoch}\0{state_hash}"
        candidate_id = "candidate_" + hashlib.sha256(key.encode()).hexdigest()
        result = await self._post(self.payload("checkpoint", agent_state=state, state_hash=state_hash,
            candidate_id=candidate_id, checkpoint_kind="progress" if progress else "waiting_inputs" if waiting_inputs else "waiting_children",
            waiting_child_task_ids=children, input_revision=self.input_revision, input_hash=self.input_hash))
        checkpoint_id = result.get("checkpoint_id")
        expected_checkpoint = "checkpoint_" + hashlib.sha256(key.encode()).hexdigest()
        if (result.get("status") != "checkpointed" or result.get("state_hash") != state_hash
                or checkpoint_id != expected_checkpoint):
            raise RemoteStateError("WORKER_STATE_CONFLICT")
        return _identifier(checkpoint_id)

    def _result_payload(self, kind: str, content: str) -> dict[str, Any]:
        if kind not in {"tool_result", "context_archive"} or not isinstance(content, str):
            raise RemoteStateError("WORKER_STATE_INVALID")
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        result_id = "result_" + hashlib.sha256(f"{kind}\0{content_hash}".encode()).hexdigest()
        return self.payload("put_result", kind=kind, content=content, content_hash=content_hash, result_id=result_id)

    @staticmethod
    def _result_ref(body: dict[str, Any], response: dict[str, Any]) -> str:
        reference = "runtime-result:" + body["result_id"]
        if response.get("result_ref") != reference or response.get("content_hash") != body["content_hash"]:
            raise RemoteStateError("WORKER_STATE_CONFLICT")
        return reference

    async def put_result(self, kind: str, content: str) -> str:
        body = self._result_payload(kind, content)
        with self._pending_lock:
            self._pending_writes.add(body["result_id"])
        reference = self._result_ref(body, await self._post(body))
        with self._pending_lock:
            self._pending_writes.discard(body["result_id"])
        return reference

    def save_text(self, text: str, encoding: str = "utf-8") -> str:
        if encoding.lower().replace("-", "") != "utf8":
            raise RemoteStateError("WORKER_STATE_INVALID")
        body = self._result_payload("tool_result", text)
        with self._pending_lock:
            self._pending_writes.add(body["result_id"])
        reference = self._result_ref(body, self._post_sync(body))
        with self._pending_lock:
            self._pending_writes.discard(body["result_id"])
        return reference

    async def get_result(self, result_ref: str, *, offset: int = 0, limit: int = 8192) -> dict[str, Any]:
        if not isinstance(result_ref, str) or not _RESULT.fullmatch(result_ref):
            raise RemoteStateError("WORKER_STATE_INVALID")
        _integer(offset)
        if type(limit) is not int or not 1 <= limit <= 32768:
            raise RemoteStateError("WORKER_STATE_INVALID")
        result = await self._post(self.payload("get_result", result_ref=result_ref, offset=offset, limit=limit))
        content = result.get("content")
        if (not isinstance(content, str) or len(content) > limit or result.get("offset") != offset
                or result.get("next_offset") != offset + len(content) or type(result.get("has_more")) is not bool
                or (result["has_more"] and not content)):
            raise RemoteStateError("WORKER_STATE_INVALID")
        return {key: result[key] for key in ("content", "offset", "next_offset", "has_more")}


async def watch_execution(client: RemoteStateClient, owner: asyncio.Task, is_open, *, interval: float = 5.0) -> None:
    """Cancel only this execution when its shared authority is lost."""
    loop = asyncio.get_running_loop()
    last_confirmed = loop.time()
    budget = client.execution.lease_budget_seconds
    while is_open() and not owner.done():
        await asyncio.sleep(interval)
        if not is_open() or owner.done():
            return
        remaining = budget - (loop.time() - last_confirmed)
        if remaining <= 0:
            owner.cancel("Runtime execution authority unavailable")
            return
        try:
            result = await asyncio.wait_for(client._post(client.payload("status")), timeout=min(interval, remaining))
            if result.get("status") != "active" or result.get("epoch") != client.execution.epoch:
                raise RemoteStateError("WORKER_EPOCH_STALE", http_status=409)
            last_confirmed = loop.time()
        except RemoteStateError as exc:
            if exc.http_status in {401, 403, 409} or exc.error_code in {"WORKER_EPOCH_STALE", "WORKER_SCOPE_MISMATCH"}:
                if is_open() and not owner.done():
                    owner.cancel("Runtime execution authority revoked")
                return
        except (TimeoutError, httpx.HTTPError):
            pass
        if loop.time() - last_confirmed >= budget and is_open() and not owner.done():
            owner.cancel("Runtime execution authority unavailable")
            return


class RemoteOffloader:
    def __init__(self, client: RemoteStateClient) -> None:
        self.client = client

    async def offload_context(self, session_id: str, msgs: list[Any]) -> str:
        from .session import _remote_agent_state
        if session_id != self.client.execution.session_id:
            raise RemoteStateError("WORKER_SCOPE_MISMATCH")
        if not msgs:
            return ""
        clean = _remote_agent_state({"state": {"context": [msg.to_dict() for msg in msgs]}})
        return await self.client.put_result("context_archive", canonical_json(clean))

    async def offload_tool_result(self, session_id: str, tool_result: Any) -> str:
        if session_id != self.client.execution.session_id:
            raise RemoteStateError("WORKER_SCOPE_MISMATCH")
        output = getattr(tool_result, "output", None) or ""
        if isinstance(output, list):
            content = "\n".join(str(block.get("text", "") if isinstance(block, dict) else getattr(block, "text", ""))
                for block in output if (block.get("type") if isinstance(block, dict) else getattr(block, "type", "")) == "text")
        else:
            content = str(output)
        return await self.client.put_result("tool_result", content)

    def cleanup_expired(self, retention_days: int = 5) -> int:
        return 0  # Runtime owns retention; a worker cannot delete shared data.
