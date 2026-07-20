# -*- coding: utf-8 -*-
"""工具合并器 —— 合并智能体 Workspace 和用户 Workspace 的工具与技能。"""
from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class ToolMerger:
    """合并两个 workspace 的工具。

    策略：
    - 用户工具**覆盖**同名智能体工具（用户自定义优先）
    - 智能体工具标记 `_source = "agent"`，用户工具标记 `_source = "user"`
    - 合并后写回 agent.toolkit.tool_groups[0].tools
    """

    def merge(
        self,
        agent: Any,
        agent_ws: Path,
        user_ws: Path,
    ) -> tuple[list[str], list[str]]:
        """合并工具列表。

        Returns:
            (removed_names, added_names): 用于审计日志
        """
        current_tools = self._get_current_tools(agent)
        current_names = {self._tool_name(t) for t in current_tools}

        user_tools = self._load_user_tools(user_ws)
        user_names = {self._tool_name(t) for t in user_tools}

        merged: dict[str, Any] = {}
        for t in current_tools:
            merged[self._tool_name(t)] = t
            if not hasattr(t, "_source"):
                try:
                    setattr(t, "_source", "agent")
                except Exception:
                    logger.debug("cannot tag tool source", exc_info=True)

        for t in user_tools:
            try:
                setattr(t, "_source", "user")
            except Exception:
                logger.debug("cannot tag tool source", exc_info=True)
            merged[self._tool_name(t)] = t  # 用户工具覆盖同名智能体工具

        toolkit = getattr(agent, "toolkit", None)
        groups = getattr(toolkit, "tool_groups", None)
        if groups:
            groups[0].tools = list(merged.values())

        final_names = set(merged.keys())
        removed = sorted(current_names - final_names)
        added = sorted(final_names - current_names)

        if removed or added:
            logger.info(
                "Tool merge: +%s -%s (agent=%d user=%d → merged=%d)",
                added,
                removed,
                len(current_names),
                len(user_names),
                len(merged),
            )

        return removed, added

    def merge_skills(
        self,
        agent: Any,
        user_ws: Path,
        channel: str = "console",
    ) -> list[dict[str, str]]:
        """把用户 workspace 中启用的 SKILL.md 技能注册进 toolkit。

        Returns:
            [{"name": ..., "dir": ...}, ...] 供 prompt 注入使用
        """
        toolkit = getattr(agent, "toolkit", None)
        if toolkit is None:
            return []

        skill_dirs = self._resolve_user_skill_dirs(user_ws, channel)
        if not skill_dirs:
            return []

        if not hasattr(toolkit, "_qp_skills"):
            toolkit._qp_skills = {}  # pylint: disable=protected-access

        registered = []
        for name, skill_dir in skill_dirs:
            # pylint: disable=protected-access
            toolkit._qp_skills[name] = {"dir": str(skill_dir)}
            register = getattr(toolkit, "register_agent_skill", None)
            if callable(register):
                try:
                    register(str(skill_dir))
                except Exception:
                    logger.debug(
                        "register_agent_skill failed for %s",
                        name,
                        exc_info=True,
                    )
            registered.append({"name": name, "dir": str(skill_dir)})

        if registered:
            logger.info(
                "User skills merged: %s",
                [s["name"] for s in registered],
            )
        return registered

    # ── 内部方法 ────────────────────────────────────────

    @staticmethod
    def _tool_name(tool: Any) -> str:
        name = getattr(tool, "name", None)
        if name:
            return str(name)
        func = getattr(tool, "func", None)
        return getattr(func, "__name__", str(tool))

    def _get_current_tools(self, agent: Any) -> list[Any]:
        """获取 Agent 当前的工具列表。"""
        toolkit = getattr(agent, "toolkit", None)
        groups = getattr(toolkit, "tool_groups", None)
        if groups:
            return list(groups[0].tools)
        return []

    def _resolve_user_skill_dirs(
        self,
        user_ws: Path,
        channel: str,
    ) -> list[tuple[str, Path]]:
        """解析用户 workspace 中启用的技能目录。"""
        skills_dir = user_ws / "skills"
        if not skills_dir.exists():
            return []

        try:
            from qwenpaw.agents.skill_system import (
                ensure_skills_initialized,
                resolve_effective_skills,
            )

            ensure_skills_initialized(user_ws)
            names = resolve_effective_skills(user_ws, channel)
            return [
                (name, skills_dir / name)
                for name in names
                if (skills_dir / name / "SKILL.md").exists()
            ]
        except Exception:
            logger.debug(
                "skill_system resolve failed; falling back to disk scan",
                exc_info=True,
            )
            return [
                (d.name, d)
                for d in sorted(skills_dir.iterdir())
                if d.is_dir() and (d / "SKILL.md").exists()
            ]

    def _load_user_tools(self, user_ws: Path) -> list[Any]:
        """从用户 workspace 加载额外的 Python 工具。

        目前通过 skills/ 目录下的 tools.py/tool.py/main.py 加载。
        未来可扩展：用户 MCP 注册的工具、用户自定义 Tool 文件。
        """
        tools = []

        skills_dir = user_ws / "skills"
        if not skills_dir.exists():
            return tools

        for skill_dir in sorted(skills_dir.iterdir()):
            if not skill_dir.is_dir():
                continue
            tools.extend(self._load_skill_tools(skill_dir))

        return tools

    def _load_skill_tools(self, skill_dir: Path) -> list[Any]:
        """从技能目录加载工具函数。"""
        tools = []
        tool_files = ["tools.py", "tool.py", "main.py"]

        for tf_name in tool_files:
            tf = skill_dir / tf_name
            if not tf.exists():
                continue
            try:
                mod = self._import_module(f"user_skill_{skill_dir.name}", tf)
                for attr_name in dir(mod):
                    attr = getattr(mod, attr_name)
                    if callable(attr) and hasattr(attr, "name"):
                        tools.append(attr)
            except Exception as e:
                logger.warning(
                    "Failed to load skill tool %s: %s",
                    skill_dir.name,
                    e,
                )

        return tools

    @staticmethod
    def _import_module(name: str, path: Path) -> Any:
        """动态导入 Python 模块。"""
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot load {path}")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod
