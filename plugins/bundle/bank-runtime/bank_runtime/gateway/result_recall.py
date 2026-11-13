"""Exact-object, read-only access to Runtime-owned result pages."""
from __future__ import annotations

import json
import re
from typing import Any

from agentscope.message import TextBlock, ToolResultState
from agentscope.tool import FunctionTool, Toolkit, ToolChunk
from qwenpaw.runtime.tool_guard import GuardedFunctionTool

from ..personalization import current_personalization_context
from ..session import current_managed_session_scope


class RuntimeResultReader:
    def __init__(self, agent: Any, gateway_client: Any) -> None:
        self.agent = agent
        self.toolkit = agent.toolkit
        self.gateway_client = gateway_client
        self.scope = current_managed_session_scope()
        self.personalization = current_personalization_context()
        self.client = self.scope.remote_client if self.scope else None
        self.tool = GuardedFunctionTool(self.read, name="read_runtime_result", is_read_only=True,
            agent_id=self.scope.agent_id if self.scope else None,
            request_context={**dict(getattr(agent, "_request_context", None) or {}),
                **({"session_id": self.scope.session_id, "user_id": self.scope.user_id,
                    "channel": self.scope.channel} if self.scope else {})},
            description="Read bounded character pages of a retained result in the current Runtime execution scope. This never opens a local file or grants access to another task.")
        self.tool_type = type(self.tool)
        self.guard_check = self.tool_type.check_permissions

    def eligible(self) -> bool:
        scope = current_managed_session_scope()
        identity = self.personalization.identity if self.personalization else None
        config = getattr(self.gateway_client, "config", None)
        return (type(self.toolkit) is Toolkit and self.agent.toolkit is self.toolkit
            and scope is not None and scope is self.scope and scope.lock_acquired
            and self.client is not None and scope.remote_client is self.client
            and identity is not None and not self.personalization.identity_error
            and current_personalization_context() is self.personalization and identity.user_id == scope.user_id
            and config is not None and config.agent_id == scope.agent_id
            and config.task_id == scope.runtime_task_id and config.session_id == scope.session_id
            and self.tool._resolve_execution_level() != "bypass")

    def recognizes(self, tool: Any) -> bool:
        return (isinstance(tool, FunctionTool) and type(tool) is self.tool_type and tool is self.tool
            and tool.name == "read_runtime_result" and tool.is_read_only and not tool.is_external_tool
            and getattr(tool.check_permissions, "__self__", None) is tool
            and getattr(tool.check_permissions, "__func__", None) is self.guard_check
            and getattr(tool._func, "__self__", None) is self
            and getattr(tool._func, "__func__", None) is RuntimeResultReader.read)

    async def visible(self) -> bool:
        if not self.eligible() or not self.recognizes(self.tool):
            return False
        try:
            return await self.toolkit.check_tool_available(self.tool.name, self.agent.state.tool_context.activated_groups) is self.tool
        except Exception:
            return False

    async def available(self, payload: dict[str, Any]) -> bool:
        return (set(payload) <= {"result_ref", "offset", "limit"}
            and isinstance(payload.get("result_ref"), str)
            and re.fullmatch(r"runtime-result:result_[a-f0-9]{64}", payload["result_ref"]) is not None
            and type(payload.get("offset", 0)) is int and 0 <= payload.get("offset", 0) <= 2**63 - 1
            and type(payload.get("limit", 8192)) is int and 1 <= payload.get("limit", 8192) <= 32768
            and await self.visible())

    async def read(self, result_ref: str, offset: int = 0, limit: int = 8192) -> ToolChunk:
        """Read a retained result page; offsets count Unicode characters.

        Args:
            result_ref: Exact result_ref returned by this execution's offloader.
            offset: Zero-based character offset, or a returned next_offset.
            limit: Maximum characters, from 1 to 32768.
        """
        payload = {"result_ref": result_ref, "offset": offset, "limit": limit}
        if not await self.available(payload):
            raise RuntimeError("Runtime result read scope is unavailable")
        result = await self.client.get_result(**payload)
        if not await self.available(payload):
            raise RuntimeError("Runtime result read scope changed")
        return ToolChunk(content=[TextBlock(text=json.dumps(result, ensure_ascii=False))], state=ToolResultState.SUCCESS)
