"""Request-scoped admission for the authenticated user's personal method reader."""
from typing import Any

from agentscope.tool import FunctionTool, Toolkit

from ..personal_skills import activate_personal_skill
from ..personalization import current_personalization_context


class PersonalSkillReader:
    def __init__(self, agent: Any) -> None:
        self.agent = agent
        self.toolkit = agent.toolkit
        self.context = current_personalization_context()
        self.tool = next((tool for group in self.toolkit.tool_groups for tool in group.tools
                          if isinstance(tool, FunctionTool) and tool._func is activate_personal_skill), None)
        self.tool_type = type(self.tool)
        self.guard_check = getattr(self.tool_type, 'check_permissions', None)

    def recognizes(self, tool: Any) -> bool:
        return (type(self.toolkit) is Toolkit and isinstance(tool, FunctionTool) and type(tool) is self.tool_type
                and tool is self.tool and tool._func is activate_personal_skill
                and tool.name == 'activate_personal_skill' and not tool.is_external_tool
                and getattr(tool.check_permissions, '__self__', None) is tool
                and getattr(tool.check_permissions, '__func__', None) is self.guard_check
                and self.agent.toolkit is self.toolkit)

    async def visible(self) -> bool:
        context = current_personalization_context()
        if (context is None or context is not self.context or context.identity is None
                or context.identity_error or context.registry is None
                or context.registry.closed or not context.registry.has_items
                or not self.recognizes(self.tool)):
            return False
        try:
            resolved = await self.toolkit.check_tool_available(
                self.tool.name, self.agent.state.tool_context.activated_groups)
            return resolved is self.tool
        except Exception:
            return False

    async def available(self, payload: dict[str, Any]) -> bool:
        return (set(payload) == {'skill_ref'} and await self.visible()
                and self.context.registry.has_reference(payload['skill_ref']))
