# -*- coding: utf-8 -*-
"""用户 Workspace 生命周期管理。"""
from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)


class UserWorkspaceManager:
    """管理用户 Workspace 的创建、初始化、清理。

    首次访问：
    - 创建目录结构
    - 从智能体 Workspace 继承初始 MCP 配置
    - 创建空的 skills/ 目录与技能清单
    - 写入 .initialized 标记
    """

    def __init__(self, users_root: Path, agents_root: Path):
        self.users_root = Path(users_root)
        self.agents_root = Path(agents_root)

    def ensure_initialized(self, user_ws: Path, agent_ws: Path) -> None:
        """确保用户 workspace 已初始化。"""
        if (user_ws / ".initialized").exists():
            return

        logger.info("Initializing user workspace: %s", user_ws)
        user_ws.mkdir(parents=True, exist_ok=True)

        (user_ws / "skills").mkdir(exist_ok=True)
        (user_ws / "workspace").mkdir(exist_ok=True)

        self._inherit_mcp_config(user_ws, agent_ws)
        self._create_default_manifest(user_ws)

        (user_ws / ".initialized").touch()
        logger.info("User workspace initialized: %s", user_ws)

    def get_skills_dir(self, user_ws: Path) -> Path:
        return user_ws / "skills"

    def get_mcp_config_path(self, user_ws: Path) -> Path:
        return user_ws / "mcp_config.json"

    def get_manifest_path(self, user_ws: Path) -> Path:
        return user_ws / "skill.json"

    def get_workdir(self, user_ws: Path) -> Path:
        """工具执行目录。"""
        wd = user_ws / "workspace"
        wd.mkdir(exist_ok=True)
        return wd

    # ── 内部方法 ────────────────────────────────────────

    def _inherit_mcp_config(self, user_ws: Path, agent_ws: Path) -> None:
        """从智能体 workspace 继承 MCP 配置作为初始模板。"""
        agent_mcp = agent_ws / "mcp_config.json"
        user_mcp = user_ws / "mcp_config.json"

        if agent_mcp.exists() and not user_mcp.exists():
            shutil.copy2(agent_mcp, user_mcp)
            logger.debug("Inherited MCP config from %s", agent_ws)

    def _create_default_manifest(self, user_ws: Path) -> None:
        """创建默认技能清单（与 skill_system 的 skill.json schema 兼容）。"""
        manifest_path = user_ws / "skill.json"
        if manifest_path.exists():
            return
        try:
            from qwenpaw.agents.skill_system import (
                ensure_skills_initialized,
            )

            ensure_skills_initialized(user_ws)
        except Exception:
            logger.debug(
                "skill_system init failed; writing minimal manifest",
                exc_info=True,
            )
            manifest_path.write_text(
                json.dumps({"skills": {}}, indent=2, ensure_ascii=False),
            )
