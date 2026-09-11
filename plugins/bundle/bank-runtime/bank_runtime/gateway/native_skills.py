"""Request-local admission for the framework's read-only Skill viewer.

This is context retrieval, not an execution capability or a name-based exemption.
The installed AgentScope viewer remains responsible for reading registered skills.
"""
from typing import Any

from agentscope.tool import Toolkit
from agentscope.tool._builtin._skill import SkillViewer


class NativeSkillReader:
    def __init__(self, agent: Any) -> None:
        self.agent = agent
        self.toolkit = agent.toolkit
        self.viewer = getattr(getattr(self.toolkit, "builtin_skill_viewer", None), "tool", None)

    def recognizes(self, tool: Any) -> bool:
        return (
            type(self.toolkit) is Toolkit
            and type(tool) is SkillViewer
            and tool.name == "Skill"
            and tool.is_read_only is True
            and tool.is_external_tool is False
            and tool is self.viewer
            and self.agent.toolkit is self.toolkit
            and self.toolkit.builtin_skill_viewer.tool is tool
            and getattr(tool._get_skills_method, "__self__", None) is self.toolkit
            and getattr(tool._get_skills_method, "__func__", None) is Toolkit._get_available_skills
        )

    async def available(self, payload: dict[str, Any]) -> bool:
        if not self.recognizes(self.viewer) or set(payload) != {"skill"}:
            return False
        name = payload["skill"]
        if not isinstance(name, str) or not name or len(name) > 256:
            return False
        groups = self.agent.state.tool_context.activated_groups
        try:
            resolved = await self.toolkit.check_tool_available("Skill", groups)
            skills = await self.viewer._get_skills_method(groups)
        except Exception:
            return False
        return resolved is self.viewer and name in skills

    async def visible(self) -> bool:
        if not self.recognizes(self.viewer):
            return False
        try:
            skills = await self.viewer._get_skills_method(self.agent.state.tool_context.activated_groups)
            return bool(skills) and await self.available({"skill": next(iter(skills))})
        except Exception:
            return False
