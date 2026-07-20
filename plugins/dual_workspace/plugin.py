# -*- coding: utf-8 -*-
"""dual_workspace 插件入口。"""
from __future__ import annotations

import logging

from .hooks import DualWorkspaceHook

logger = logging.getLogger(__name__)


class DualWorkspacePlugin:
    """智能体 Workspace（只读）+ 用户 Workspace（读写）非侵入合并。"""

    def register(self, api) -> None:
        hook = DualWorkspaceHook()
        api.register_runtime_hook(hook)
        logger.info("dual_workspace plugin registered")


plugin = DualWorkspacePlugin()
