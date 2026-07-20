# -*- coding: utf-8 -*-
"""DualWorkspaceHook — 智能体 + 用户双 Workspace 合并。"""
from __future__ import annotations

import logging
import os
import time as time_module
from pathlib import Path

from qwenpaw.runtime.hooks import HookBase, HookContext, HookResult
from qwenpaw.runtime.phases import Phase

from .tool_merger import ToolMerger
from .workspace_manager import UserWorkspaceManager

logger = logging.getLogger(__name__)


def _default_root(env_var: str, fallback_name: str) -> Path:
    override = os.environ.get(env_var)
    if override:
        return Path(override).expanduser().resolve()
    try:
        from qwenpaw.constant import WORKING_DIR

        return Path(WORKING_DIR) / fallback_name
    except Exception:
        return Path.home() / ".qwenpaw" / fallback_name


def agents_workspace_root() -> Path:
    return _default_root("DUAL_WORKSPACE_AGENTS_ROOT", "agents")


def users_workspace_root() -> Path:
    return _default_root("DUAL_WORKSPACE_USERS_ROOT", "users")


def _safe_segment(value: str) -> str:
    """把 user_id / agent_id 转为安全的目录名，防止路径穿越。"""
    cleaned = "".join(
        c if (c.isalnum() or c in "._-") else "_" for c in value.strip()
    )
    cleaned = cleaned.replace("..", "_").strip("._")
    return cleaned or "_"


class DualWorkspaceHook(HookBase):
    """POST_AGENT_BUILD 阶段合并双 Workspace。

    职责：
    1. 根据 user_id 初始化用户 workspace
    2. 切换 ctx.workspace_dir 到用户 workspace（并同步 ContextVar，
       工具执行时通过 get_current_workspace_dir() 读取）
    3. 合并智能体工具 + 用户工具
    """

    phase = Phase.POST_AGENT_BUILD
    name = "dual_workspace"
    priority = 50

    def __init__(
        self,
        users_root: Path | None = None,
        agents_root: Path | None = None,
    ):
        self._agents_root = agents_root or agents_workspace_root()
        self._users_root = users_root or users_workspace_root()
        self._user_ws_manager = UserWorkspaceManager(
            users_root=self._users_root,
            agents_root=self._agents_root,
        )
        self._tool_merger = ToolMerger()

    # ── Hook 入口 ──────────────────────────────────────

    async def run(self, ctx: HookContext) -> HookResult:
        agent_id = _safe_segment(ctx.root_agent_id or ctx.agent_id or "")
        user_id = self._resolve_user_id(ctx)
        if not user_id or not agent_id:
            return HookResult()

        t_start = time_module.monotonic()

        agent_ws = self._agents_root / agent_id
        user_ws = self._users_root / user_id

        if not agent_ws.exists():
            logger.warning("Agent workspace not found: %s", agent_ws)

        try:
            self._user_ws_manager.ensure_initialized(user_ws, agent_ws)
        except Exception:
            logger.error(
                "Failed to init user workspace %s",
                user_ws,
                exc_info=True,
            )
            return HookResult()

        # ⭐ 核心：切换执行目录到用户 workspace。
        # ContextVarsSetupHook 已在 PRE_DISPATCH 把旧目录写入 ContextVar，
        # 工具（shell/file_io 等）在执行时读取 ContextVar，因此必须同步更新。
        ctx.workspace_dir = user_ws
        self._sync_workspace_contextvar(user_ws)

        if not os.access(user_ws, os.W_OK):
            logger.error("User workspace not writable: %s", user_ws)
            ctx.extras["dual_workspace_readonly"] = True

        removed, added = self._tool_merger.merge(
            agent=ctx.agent,
            agent_ws=agent_ws,
            user_ws=user_ws,
        )

        channel = getattr(ctx.request, "channel", None) or "console"
        user_skills = self._tool_merger.merge_skills(
            agent=ctx.agent,
            user_ws=user_ws,
            channel=channel,
        )
        if user_skills:
            lines = ["The user has the following personal skills available:"]
            lines.extend(
                f"- {s['name']} (dir: {s['dir']})" for s in user_skills
            )
            ctx.inject_context(
                "\n".join(lines),
                priority=60,
                source="dual_workspace",
            )

        elapsed = (time_module.monotonic() - t_start) * 1000
        ctx.extras["dual_workspace"] = {
            "agent_id": agent_id,
            "user_id": user_id,
            "agent_workspace": str(agent_ws),
            "user_workspace": str(user_ws),
            "execution_dir": str(user_ws),
            "tools_removed": removed,
            "tools_added": added,
            "user_skills": [s["name"] for s in user_skills],
            "hook_duration_ms": elapsed,
        }

        logger.info(
            "Dual WS merged: agent=%s user=%s dir=%s (%.1fms)",
            agent_id,
            user_id,
            user_ws,
            elapsed,
        )
        return HookResult()

    # ── 私有方法 ────────────────────────────────────────

    def _resolve_user_id(self, ctx: HookContext) -> str | None:
        """提取用户 ID：request.user_id > channel_meta.sender_id > session_id。"""
        req = ctx.request
        uid = getattr(req, "user_id", None)
        if uid:
            raw = uid
        else:
            meta = getattr(req, "channel_meta", None)
            raw = meta.get("sender_id") if isinstance(meta, dict) else None
        if not raw:
            raw = ctx.session_id
        if not raw:
            return None
        return _safe_segment(str(raw))

    @staticmethod
    def _sync_workspace_contextvar(user_ws: Path) -> None:
        try:
            from qwenpaw.config.context import set_current_workspace_dir

            set_current_workspace_dir(user_ws)
        except Exception:
            logger.debug(
                "set_current_workspace_dir failed",
                exc_info=True,
            )
