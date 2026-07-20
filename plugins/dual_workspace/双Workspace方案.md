# 双 Workspace 方案 — 基于 Hook 非侵入实现

> **适用版本**：QwenPaw 2.0.0.post1
> **侵入程度**：零（独立 Plugin 包）
> **核心 Hook**：`POST_AGENT_BUILD`
> **依赖**：无需外部服务

---

## 一、目标

在不修改 QwenPaw 核心代码的前提下，实现两层 Workspace 架构：

```
┌──────────────────────────────────────────┐
│         智能体 Workspace（只读）           │
│   /agents/{agent_id}/                     │
│   ├── skills/         ← 智能体专属技能     │
│   ├── mcp_config.json  ← 智能体 MCP 连接   │
│   └── 不可被用户写                        │
├──────────────────────────────────────────┤
│          用户 Workspace（读写）            │
│   /users/{user_id}/                       │
│   ├── skills/         ← 用户自定义技能     │
│   ├── mcp_config.json  ← 用户 MCP 连接     │
│   └── 工具执行目录                        │
└──────────────────────────────────────────┘
```

**关键约束**：
- 智能体 Workspace **不可写**——存放智能体（如智能问数、智能审批）的技能和 MCP
- 用户 Workspace **可读写**——每个用户独立，存放自定义技能和 MCP，同时也是工具执行目录
- 同一个智能体被多个用户使用时，智能体 Workspace 共享，用户 Workspace 隔离

---

## 二、现有架构分析

### 2.1 Workspace 数据结构

```python
# src/qwenpaw/app/workspace/workspace.py
class Workspace:
    def __init__(self, agent_id: str, workspace_dir: str):
        self.agent_id = agent_id
        self.workspace_dir = Path(workspace_dir)   # ← 文件系统根目录
        self.plugins = WorkspacePlugins()           # ← 工具/Hook/命令注册表
        self._local_workspace = QwenPawLocalWorkspace(
            tool_registry=self.plugins.tool_registry,
            workdir=str(self.workspace_dir),        # ← AgentScope 的工作目录
        )
```

### 2.2 现有两层设计

QwenPaw 已有两层：

```
skill_pool/（全局共享）              ← 类似「智能体 Workspace」
    ├── cron/
    ├── pdf/
    └── docx/

workspaces/{agent_id}/（每个 Agent） ← 当前是 agent 粒度，需要改为 user 粒度
    ├── skills/
    └── skill.json
```

**需要做的**：将 `workspaces/{agent_id}/` 拆分为：
- `agents/{agent_id}/` — 智能体的只读技能/MCP
- `users/{user_id}/` — 用户的可读写技能/MCP + 执行目录

### 2.3 工具执行目录

```python
# Agent 执行时，工具的工作目录来自 workspace.workspace_dir
# shell 工具执行在 workdir 下
# 文件读写工具也基于 workdir
ctx.workspace_dir  # ← Hook 中可以修改！
```

**关键**：`ctx.workspace_dir` 在 `HookContext` 中可赋值，修改后工具执行目录随之改变。

---

## 三、设计方案

### 3.1 整体流程

```
请求到达（user_id = "user_123", agent_id = "智能问数"）
    │
    ├─ [1] PRE_DISPATCH
    │       └─ 提取 user_id → ctx.extras["user_id"]
    │
    ├─ [2-3-4] ... → AgentBuilder.build()
    │       └─ 使用 workspace.local_workspace.list_tools()
    │          （此时还是智能体的工具）
    │
    ├─ [5] POST_AGENT_BUILD  ← ⭐ 核心 Hook
    │       │
    │       ├─ Step 1: 初始化用户 workspace（首次访问）
    │       ├─ Step 2: 切换 ctx.workspace_dir → 用户 workspace
    │       ├─ Step 3: 加载用户 workspace 的工具
    │       ├─ Step 4: 合并智能体 + 用户工具
    │       └─ Step 5: 标记来源
    │
    ├─ [6] PRE_EXECUTE → Agent 执行
    │       └─ 工具在用户 workspace 目录下执行 ✅
    │
    └─ [7-8] POST_RESPONSE / FINALLY
            └─ 文件产物留在用户 workspace
```

### 3.2 文件系统布局

```
~/.qwenpaw/
│
├── agents/                              ← 智能体 Workspace（只读）
│   ├── 智能问数/
│   │   ├── skills/
│   │   │   ├── sql_query/               ← 问数专用技能
│   │   │   └── data_viz/
│   │   ├── mcp_config.json              ← 数据库连接配置
│   │   └── READONLY                      ← 只读标记文件
│   │
│   ├── 智能审批/
│   │   ├── skills/
│   │   │   └── approval_flow/
│   │   ├── mcp_config.json
│   │   └── READONLY
│   │
│   └── 智能问答/
│       ├── skills/
│       │   └── kb_search/
│       ├── mcp_config.json
│       └── READONLY
│
├── users/                               ← 用户 Workspace（读写）
│   ├── user_001/
│   │   ├── skills/                      ← 用户自定义技能
│   │   │   └── my_report/
│   │   ├── mcp_config.json              ← 用户自己的 MCP
│   │   ├── skill.json                   ← 技能启用/频道配置
│   │   ├── .initialized                 ← 初始化标记
│   │   ├── token_usage.json             ← Token 统计
│   │   └── workspace/                   ← 工具执行产生的文件
│   │       ├── output/
│   │       └── downloads/
│   │
│   └── user_002/
│       └── ...
│
├── skill_pool/                          ← 全局技能池（现有，不变）
│   ├── cron/
│   ├── pdf/
│   └── docx/
│
└── config/                              ← 全局配置
    └── agents.yaml                      ← 智能体定义
```

---

## 四、完整实现

### 4.1 文件结构

```
dual_workspace/
├── plugin.py                # 插件入口
├── manifest.yaml             # 插件声明
├── hooks.py                  # DualWorkspaceHook
├── workspace_manager.py      # 用户 workspace 管理
└── tool_merger.py            # 工具合并逻辑
```

### 4.2 核心 Hook (`hooks.py`)

```python
"""DualWorkspaceHook — 智能体 + 用户双 Workspace 合并。"""
import logging
import time as time_module
from pathlib import Path
from typing import Any

from qwenpaw.runtime.hooks import HookBase, HookContext, HookResult, Phase

from .workspace_manager import UserWorkspaceManager
from .tool_merger import ToolMerger

logger = logging.getLogger(__name__)

# 目录配置（可通过环境变量覆盖）
AGENTS_WORKSPACE_ROOT = Path.home() / ".qwenpaw" / "agents"
USERS_WORKSPACE_ROOT = Path.home() / ".qwenpaw" / "users"


class DualWorkspaceHook(HookBase):
    """POST_AGENT_BUILD 阶段合并双 Workspace。

    职责：
    1. 根据 user_id 初始化用户 workspace
    2. 切换 ctx.workspace_dir 到用户 workspace
    3. 合并智能体工具 + 用户工具
    """

    phase = Phase.POST_AGENT_BUILD
    name = "dual_workspace"
    priority = 50  # 早于 UserToolFilterHook(100)

    def __init__(self):
        self._user_ws_manager = UserWorkspaceManager(
            users_root=USERS_WORKSPACE_ROOT,
            agents_root=AGENTS_WORKSPACE_ROOT,
        )
        self._tool_merger = ToolMerger()

    # ── Hook 入口 ──────────────────────────────────────

    async def run(self, ctx: HookContext) -> HookResult:
        # 1. 身份识别
        agent_id = ctx.root_agent_id or ctx.agent_id
        user_id = self._resolve_user_id(ctx)
        if not user_id or not agent_id:
            return HookResult()

        t_start = time_module.monotonic()

        # 2. 确定路径
        agent_ws = AGENTS_WORKSPACE_ROOT / agent_id
        user_ws = USERS_WORKSPACE_ROOT / user_id

        # 3. 初始化用户 workspace（首次访问）
        self._user_ws_manager.ensure_initialized(user_ws, agent_ws)

        # 4. ⭐ 核心：切换执行目录到用户 workspace
        #    此后所有工具（shell/file_read/write 等）都在此目录下执行
        ctx.workspace_dir = user_ws

        # 5. 合并工具列表
        removed, added = self._tool_merger.merge(
            agent=ctx.agent,
            agent_ws=agent_ws,
            user_ws=user_ws,
        )

        # 6. 记录元数据
        elapsed = (time_module.monotonic() - t_start) * 1000
        ctx.extras["dual_workspace"] = {
            "agent_id": agent_id,
            "user_id": user_id,
            "agent_workspace": str(agent_ws),
            "user_workspace": str(user_ws),
            "execution_dir": str(user_ws),
            "tools_removed": removed,
            "tools_added": added,
            "hook_duration_ms": elapsed,
        }

        logger.info(
            "Dual WS merged: agent=%s user=%s dir=%s (%.1fms)",
            agent_id, user_id, user_ws, elapsed,
        )
        return HookResult()

    # ── 私有方法 ────────────────────────────────────────

    def _resolve_user_id(self, ctx: HookContext) -> str | None:
        """提取用户 ID，优先级：request.user_id > channel_meta.sender_id > session_id。"""
        req = ctx.request
        if uid := getattr(req, "user_id", None):
            return uid
        if meta := getattr(req, "channel_meta", None):
            if sender := getattr(meta, "sender_id", None):
                return sender
        return ctx.session_id
```

### 4.3 用户 Workspace 管理 (`workspace_manager.py`)

```python
"""用户 Workspace 生命周期管理。"""
import json
import logging
import shutil
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class UserWorkspaceManager:
    """管理用户 Workspace 的创建、初始化、清理。

    首次访问：
    - 创建目录结构
    - 从智能体 Workspace 继承初始 MCP 配置
    - 创建空的 skills/ 目录
    - 写入 .initialized 标记
    """

    def __init__(self, users_root: Path, agents_root: Path):
        self.users_root = users_root
        self.agents_root = agents_root

    def ensure_initialized(self, user_ws: Path, agent_ws: Path) -> None:
        """确保用户 workspace 已初始化。"""
        if (user_ws / ".initialized").exists():
            return

        logger.info("Initializing user workspace: %s", user_ws)
        user_ws.mkdir(parents=True, exist_ok=True)

        # 1. 创建 skills 目录
        (user_ws / "skills").mkdir(exist_ok=True)

        # 2. 创建 workspace 执行目录
        (user_ws / "workspace").mkdir(exist_ok=True)

        # 3. 从智能体 workspace 继承 MCP 配置作为初始模板
        self._inherit_mcp_config(user_ws, agent_ws)

        # 4. 创建空的技能清单
        self._create_default_manifest(user_ws)

        # 5. 写入初始化标记
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
        """从智能体 workspace 继承 MCP 配置。"""
        agent_mcp = agent_ws / "mcp_config.json"
        user_mcp = user_ws / "mcp_config.json"

        if agent_mcp.exists() and not user_mcp.exists():
            shutil.copy2(agent_mcp, user_mcp)
            logger.debug("Inherited MCP config from %s", agent_ws)

    def _create_default_manifest(self, user_ws: Path) -> None:
        """创建默认技能清单。"""
        manifest_path = user_ws / "skill.json"
        if manifest_path.exists():
            return
        manifest_path.write_text(
            json.dumps({"skills": {}}, indent=2, ensure_ascii=False),
        )
```

### 4.4 工具合并器 (`tool_merger.py`)

```python
"""工具合并器 —— 合并智能体 Workspace 和用户 Workspace 的工具列表。"""
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
        # 1. 获取当前 Agent 的工具（已从智能体 workspace 加载）
        current_tools = self._get_current_tools(agent)
        current_names = {t.name for t in current_tools}

        # 2. 加载用户 workspace 的额外工具
        user_tools = self._load_user_tools(user_ws)
        user_names = {t.name for t in user_tools}

        # 3. 合并（用户覆盖）
        merged = {}
        for t in current_tools:
            merged[t.name] = t
            # 标记来源
            if not hasattr(t, "_source"):
                setattr(t, "_source", "agent")

        for t in user_tools:
            setattr(t, "_source", "user")
            merged[t.name] = t  # 用户工具覆盖同名智能体工具

        # 4. 写回
        toolkit = getattr(agent, "toolkit", None)
        if toolkit and toolkit.tool_groups:
            toolkit.tool_groups[0].tools = list(merged.values())

        # 5. 计算变化
        final_names = set(merged.keys())
        removed = sorted(current_names - final_names)
        added = sorted(final_names - current_names)

        if removed or added:
            logger.info(
                "Tool merge: +%s -%s (agent=%d user=%d → merged=%d)",
                added, removed,
                len(current_names), len(user_names), len(merged),
            )

        return removed, added

    # ── 内部方法 ────────────────────────────────────────

    def _get_current_tools(self, agent: Any) -> list[Any]:
        """获取 Agent 当前的工具列表。"""
        toolkit = getattr(agent, "toolkit", None)
        if toolkit and toolkit.tool_groups:
            return list(toolkit.tool_groups[0].tools)
        return []

    def _load_user_tools(self, user_ws: Path) -> list[Any]:
        """从用户 workspace 加载额外的工具。

        目前通过 skills/ 目录加载。未来可扩展：
        - 用户 MCP 注册的工具
        - 用户自定义 Tool 文件
        """
        tools = []

        # 加载用户 skills 目录下的工具
        skills_dir = user_ws / "skills"
        if not skills_dir.exists():
            return tools

        for skill_dir in skills_dir.iterdir():
            if not skill_dir.is_dir():
                continue
            # 尝试加载技能注册的工具
            skill_tools = self._load_skill_tools(skill_dir)
            tools.extend(skill_tools)

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
                    "Failed to load skill tool %s: %s", skill_dir.name, e,
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
```

### 4.5 插件入口 (`plugin.py`)

```python
"""dual_workspace 插件入口。"""
import os

from qwenpaw.plugins.api import PluginAPI

from .hooks import DualWorkspaceHook

api = PluginAPI()


def setup(api: PluginAPI) -> None:
    """加载双 Workspace Hook。"""
    hook = DualWorkspaceHook()
    api.register_runtime_hook(hook)
```

### 4.6 插件声明 (`manifest.yaml`)

```yaml
id: "dual-workspace"
version: "1.0.0"
name: "双 Workspace 架构"
description: "智能体 Workspace（只读）+ 用户 Workspace（读写）非侵入合并"
plugin_type: "general"
entry:
  backend: "plugin.py"
```

---

## 五、安全设计

### 5.1 智能体 Workspace 只读保证

| 层面 | 措施 |
|------|------|
| **文件系统** | 智能体 workspace 在 `~/.qwenpaw/agents/` 下，通过 `READONLY` 标记文件 + 可选的文件权限（chmod 444） |
| **ctx.workspace_dir** | Hook 中显式设置为用户 workspace，工具执行不会写智能体目录 |
| **MCP 配置** | 继承时 `shutil.copy2`，之后用户只能改自己的那份 |
| **Console 前端** | 读取 `dual_workspace` extras 元数据，隐藏智能体 workspace 的编辑入口 |

### 5.2 用户隔离

| 层面 | 措施 |
|------|------|
| **文件系统** | 不同用户 `user_id` 映射到不同目录 |
| **内存** | 每次请求独立构建 Agent，无跨用户泄漏 |
| **Session** | 每个用户的 Session 存储在自己的 workspace 下 |

### 5.3 兜底策略

```python
# 智能体 workspace 不存在 → 跳过（只用用户 workspace）
if not agent_ws.exists():
    logger.warning("Agent workspace not found: %s", agent_ws)
    ctx.workspace_dir = user_ws
    return HookResult()

# 用户 workspace 不可写 → 降级为只读模式
if not os.access(user_ws, os.W_OK):
    logger.error("User workspace not writable: %s", user_ws)
    # 仍然切换目录，但标记只读
    ctx.extras["dual_workspace_readonly"] = True
```

---

## 六、与现有系统的兼容

### 6.1 技能系统

```python
# 现有技能加载路径：
# workspaces/{agent_id}/skills/  → 改为 → users/{user_id}/skills/

# skill.json 清单：
# workspaces/{agent_id}/skill.json  → 改为 → users/{user_id}/skill.json

# SkillService 初始化时传入用户 workspace_dir：
from qwenpaw.agents.skill_system.workspace_service import SkillService
service = SkillService(workspace_dir=user_ws)
```

### 6.2 Console 前端适配

```javascript
// 前端读取 ctx.extras["dual_workspace"] 区分：
// - agent_workspace 下的技能 → 显示为「系统内置」、灰色、不可编辑
// - user_workspace 下的技能 → 显示为「我的技能」、可编辑

// 创建/编辑技能时，API 路由到 users/{user_id}/skills/
```

### 6.3 Token 统计

```python
# Token 使用量按用户 workspace 存储：
# users/{user_id}/token_usage.json  （而非 agents/{agent_id}/token_usage.json）
```

---

## 七、部署

```bash
# 1. 创建目录结构
mkdir -p ~/.qwenpaw/agents/智能问数/{skills,mcp_config.json}
mkdir -p ~/.qwenpaw/agents/智能审批/{skills,mcp_config.json}
mkdir -p ~/.qwenpaw/agents/智能问答/{skills,mcp_config.json}
mkdir -p ~/.qwenpaw/users/

# 2. 部署智能体技能
cp -r /path/to/sql_query_skill ~/.qwenpaw/agents/智能问数/skills/

# 3. 安装插件
cp -r dual_workspace ~/.qwenpaw/plugins/

# 4. 重启
qwenpaw app
```

---

## 八、核心流程总结

```
一次 Agent 请求的 Workspace 切换流程：

  PRE_DISPATCH    →  user_id 尚无，继续用 agent workspace
  POST_DISPATCH   →  同上
  PRE_AGENT_BUILD →  同上（Session 仍在 agent workspace）
  AgentBuilder    →  从 agent workspace 加载工具（智能体技能 + 全局技能池）
  POST_AGENT_BUILD →  ⭐ 本 Hook：
  │                   ① user_ws = ~/.qwenpaw/users/user_123/
  │                   ② ctx.workspace_dir = user_ws    ← 执行目录切换
  │                   ③ 加载 user_ws/skills/ 下的工具
  │                   ④ 合并：agent 工具 + user 工具
  │                   ⑤ 写回 agent.toolkit.tool_groups[0].tools
  PRE_EXECUTE     →  Agent 执行，工具在 user_ws 目录下工作 ✅
  POST_RESPONSE   →  文件产物留在 user_ws/workspace/
  FINALLY         →  Session 保存（可在后续优化中保存到 user_ws）
```

**零侵入、可热插拔、完全独立。**

---

## 九、实现备注（与本文档的偏差）

插件已实现于本目录（`plugin.py` / `hooks.py` / `workspace_manager.py` / `tool_merger.py` / `plugin.json`），与上文示例代码有以下必要偏差：

1. **插件声明为 `plugin.json`**（非 `manifest.yaml`）——QwenPaw PluginLoader 只识别 JSON 清单。
2. **入口契约为导出 `plugin` 对象**（含 `register(api)` 方法），而非 `setup(api)` 函数；`PluginApi` 类名非 `PluginAPI`。
3. **必须同步 ContextVar**：`ContextVarsSetupHook` 在 PRE_DISPATCH 已把旧 `workspace_dir` 写入 ContextVar，工具（shell/file_io 等）执行时读取的是 ContextVar，因此 Hook 内除 `ctx.workspace_dir = user_ws` 外还调用 `set_current_workspace_dir(user_ws)`。
4. **`channel_meta` 是 dict**：`sender_id` 通过 `meta.get("sender_id")` 读取。
5. **路径安全**：`user_id` / `agent_id` 经 `_safe_segment()` 消毒（折叠 `..`、替换分隔符），防止路径穿越。
6. **技能合并**：用户技能遵循 QwenPaw 现有 `skill.json` 启用语义（`enabled: true` 才生效），注册进 `toolkit._qp_skills` 并通过 `ctx.inject_context()` 注入系统提示；Python 工具文件（tools.py 等）按原文档方式合并。
7. **目录根**：默认 `WORKING_DIR/agents`、`WORKING_DIR/users`，可用环境变量 `DUAL_WORKSPACE_AGENTS_ROOT` / `DUAL_WORKSPACE_USERS_ROOT` 覆盖。

测试：`tests/unit/plugins/dual_workspace/test_dual_workspace.py`（12 例）。
