"""Runtime-owned expert delegation with durable coordinator suspension."""
from __future__ import annotations

from typing import Any

from agentscope.message import TextBlock, ToolResultState
from agentscope.tool import FunctionTool, ToolChunk
from qwenpaw.runtime.control import RuntimeSuspension
from qwenpaw.runtime.tool_guard import GuardedFunctionTool

from .result_recall import RuntimeResultReader
from ..remote_state import RemoteStateClient, RemoteStateError, canonical_hash, canonical_json, _identifier, _integer
from ..session import _remote_agent_state

_TERMINAL = frozenset({"completed", "succeeded", "success", "failed", "cancelled", "canceled",
    "timeout", "timed_out", "execution_unknown", "blocked", "rejected", "stopped"})


class DelegationClient(RemoteStateClient):
    endpoint = "/runtime/v1/worker-delegations"

    async def delegate(self, role_id: str, instruction: str, context_refs: list[str] | None = None,
                       version: int | None = None, idempotency_key: str = "", purpose: str = "analysis") -> dict[str, Any]:
        role_id = _identifier(role_id)
        if not isinstance(instruction, str) or not instruction.strip() or len(instruction) > 32768:
            raise RemoteStateError("WORKER_STATE_INVALID")
        refs = context_refs if context_refs is not None else []
        if not isinstance(refs, list) or len(refs) > 64 or any(not isinstance(ref, str) or not 1 <= len(ref) <= 2048 for ref in refs):
            raise RemoteStateError("WORKER_STATE_INVALID")
        if version is not None:
            _integer(version, 1)
        if purpose not in {"analysis", "review"}:
            raise RemoteStateError("WORKER_STATE_INVALID")
        values = {"role_id": role_id, "instruction": instruction, "context_refs": refs,
            "purpose": purpose,
            **({"version": version} if version is not None else {})}
        values["idempotency_key"] = (_identifier(idempotency_key) if idempotency_key else
            "delegate_" + canonical_hash({"execution_id": self.execution.execution_id, **values}))
        result = await self._post(self.payload("delegate", **values))
        role = result.get("role")
        if (not isinstance(role, dict) or role.get("role_id") != role_id
                or not isinstance(role.get("name"), str) or len(role["name"]) > 256
                or result.get("status") != "queued"):
            raise RemoteStateError("WORKER_STATE_INVALID")
        _integer(role.get("version"), 1)
        if version is not None and version != role["version"]:
            raise RemoteStateError("WORKER_STATE_CONFLICT")
        return {"child_task_id": _identifier(result.get("child_task_id")), "status": "queued",
            "branch_id": _identifier(result.get("branch_id")),
            "role": {"role_id": role_id, "version": role["version"], "name": role["name"]}}

    async def results(self, child_task_ids: list[str]) -> dict[str, Any]:
        if not isinstance(child_task_ids, list) or not 1 <= len(child_task_ids) <= 64:
            raise RemoteStateError("WORKER_STATE_INVALID")
        ids = [_identifier(item) for item in child_task_ids]
        if len(set(ids)) != len(ids):
            raise RemoteStateError("WORKER_STATE_INVALID")
        response = await self._post(self.payload("results", child_task_ids=ids))
        items = response.get("items")
        if not isinstance(items, list) or len(items) != len(ids):
            raise RemoteStateError("WORKER_STATE_INVALID")
        projected = []
        for item in items:
            if not isinstance(item, dict) or item.get("child_task_id") not in ids:
                raise RemoteStateError("WORKER_SCOPE_MISMATCH")
            status = item.get("status")
            if status not in _TERMINAL | {"queued", "dispatching", "running", "waiting", "waiting_children", "pending"}:
                raise RemoteStateError("WORKER_STATE_INVALID")
            value = {"child_task_id": item["child_task_id"], "role_id": _identifier(item.get("role_id")),
                "version": _integer(item.get("version"), 1), "status": status}
            if "result" in item:
                result = item["result"]
                if (not isinstance(result, dict) or not isinstance(result.get("text"), str)
                        or len(result["text"]) > 262144 or not isinstance(result.get("refs", []), list)
                        or len(result.get("refs", [])) > 64
                        or any(not isinstance(ref, str) or len(ref) > 2048 for ref in result.get("refs", []))):
                    raise RemoteStateError("WORKER_STATE_INVALID")
                value["result"] = {"text": result["text"], "refs": result.get("refs", [])}
            if item.get("error_code"):
                value["error_code"] = _identifier(item["error_code"])
            projected.append(value)
        if {item["child_task_id"] for item in projected} != set(ids):
            raise RemoteStateError("WORKER_SCOPE_MISMATCH")
        return {"items": projected}


class RuntimeDelegationTools(RuntimeResultReader):
    """Identity/ToolGuard scope matches the exact controlled result reader."""
    def __init__(self, agent: Any, gateway_client: Any) -> None:
        super().__init__(agent, gateway_client)
        self.delegation_client = DelegationClient(self.client.execution) if self.client else None
        self.outstanding: set[str] = set()
        self.pending_read = False
        request_context = dict(getattr(self.tool, "_request_context", None) or
            getattr(agent, "_request_context", None) or {})
        if self.scope is not None:
            request_context.update(session_id=self.scope.session_id, user_id=self.scope.user_id, channel=self.scope.channel)
        self.delegate_tool = GuardedFunctionTool(self.delegate, name="delegate_runtime_expert", is_read_only=False,
            agent_id=self.scope.agent_id if self.scope else None, request_context=request_context,
            description="Delegate a bounded task to a published Runtime expert. Runtime checks role and resource authority and creates an independent child task. Multiple independent experts may be delegated before reading their results; do not treat queued work as completed.")
        self.results_tool = GuardedFunctionTool(self.read_results, name="read_runtime_expert_results", is_read_only=True,
            agent_id=self.scope.agent_id if self.scope else None, request_context=request_context,
            description="Read results of this coordinator's expert child tasks. If any requested child is unfinished, this coordinator checkpoints and releases its worker until the children finish. Do not busy-poll or invent results.")
        self.tools = (self.delegate_tool, self.results_tool)
        self._tool_types = {tool.name: type(tool) for tool in self.tools}

    def eligible(self) -> bool:
        return super().eligible() and not self.scope.coordination.get("role_snapshot")

    def recognizes(self, tool: Any) -> bool:
        expected = {"delegate_runtime_expert": (self.delegate_tool, RuntimeDelegationTools.delegate, False),
            "read_runtime_expert_results": (self.results_tool, RuntimeDelegationTools.read_results, True)}.get(getattr(tool, "name", ""))
        return bool(expected and isinstance(tool, FunctionTool) and type(tool) is self._tool_types.get(tool.name)
            and tool is expected[0] and tool.is_read_only is expected[2] and not tool.is_external_tool
            and getattr(tool.check_permissions, "__self__", None) is tool
            and getattr(tool.check_permissions, "__func__", None) is self.guard_check
            and getattr(tool._func, "__self__", None) is self and getattr(tool._func, "__func__", None) is expected[1])

    async def visible_names(self) -> set[str]:
        if not self.eligible():
            return set()
        visible = set()
        for tool in self.tools:
            if self.recognizes(tool) and await self.toolkit.check_tool_available(tool.name, self.agent.state.tool_context.activated_groups) is tool:
                visible.add(tool.name)
        return visible

    async def available_call(self, name: str, payload: dict[str, Any]) -> bool:
        if name not in await self.visible_names():
            return False
        if name == "read_runtime_expert_results":
            ids = payload.get("child_task_ids")
            return (set(payload) == {"child_task_ids"} and isinstance(ids, list) and 1 <= len(ids) <= 64
                and all(isinstance(item, str) and 1 <= len(item) <= 192 for item in ids))
        return (set(payload) <= {"role_id", "instruction", "context_refs", "version", "idempotency_key", "purpose"}
            and isinstance(payload.get("role_id"), str) and isinstance(payload.get("instruction"), str)
            and 1 <= len(payload["instruction"]) <= 32768
            and (payload.get("context_refs") is None or isinstance(payload["context_refs"], list))
            and (payload.get("version") is None or type(payload["version"]) is int)
            and isinstance(payload.get("idempotency_key", ""), str)
            and payload.get("purpose", "analysis") in {"analysis", "review"})

    async def delegate(self, role_id: str, instruction: str, context_refs: list[str] | None = None,
                       version: int | None = None, idempotency_key: str = "", purpose: str = "analysis") -> ToolChunk:
        """Delegate work; context_refs identifies existing authorized resources only."""
        values = dict(role_id=role_id, instruction=instruction, context_refs=context_refs,
            version=version, idempotency_key=idempotency_key, purpose=purpose)
        if not await self.available_call("delegate_runtime_expert", values):
            raise RemoteStateError("WORKER_SCOPE_MISMATCH")
        result = await self.delegation_client.delegate(**values)
        self.outstanding.add(result["child_task_id"])
        return ToolChunk(content=[TextBlock(text=canonical_json(result))], state=ToolResultState.SUCCESS)

    async def read_results(self, child_task_ids: list[str]) -> ToolChunk:
        """Read this coordinator's child tasks; unfinished work triggers durable suspension."""
        if not await self.available_call("read_runtime_expert_results", {"child_task_ids": child_task_ids}):
            raise RemoteStateError("WORKER_SCOPE_MISMATCH")
        result = await self.delegation_client.results(child_task_ids)
        for item in result["items"]:
            if item["status"] in _TERMINAL:
                self.outstanding.discard(item["child_task_id"])
            else:
                self.outstanding.add(item["child_task_id"])
                self.pending_read = True
        return ToolChunk(content=[TextBlock(text=canonical_json(result))], state=ToolResultState.SUCCESS)

    async def suspend_if_needed(self, agent: Any, *, final: bool = False) -> None:
        if not self.outstanding or not (self.pending_read or final):
            return
        if agent is not self.agent or not self.eligible() or getattr(self.gateway_client, "pending_acknowledgements", None):
            raise RemoteStateError("WORKER_SCOPE_MISMATCH")
        children = sorted(self.outstanding)
        checkpoint_id = await self.client.checkpoint(_remote_agent_state(agent.state_dict()), children)
        self.scope.commit_allowed = False
        raise RuntimeSuspension(checkpoint_id, children)
