# Tool Gateway + 用户工作区隔离 设计方案

## 一、需求概述

在 QwenPaw 中增加两项能力：
1. **Tool Gateway**：基于 `user_id` 从 Gateway 端点获取该用户可用的工具列表，**包括内置工具在内的所有工具**未获授权均不可使用
2. **用户工作区隔离**：每个 `user_id` 路由到自己的独立工作区，不可见其他用户的数据（会话、记忆、文件、凭证等）

Gateway 端点基于 MCP 协议设计。

---

## 二、现状分析

### 2.1 当前工具管线

```
Channel → AgentRequest(user_id, channel) → AgentBuilder.build()
  → _build_request_context()       # user_id → request_context
  → build_toolkit()                 # 工具组装核心
    → local_ws.list_tools()         # 内置工具 (ToolRegistry.filter)
    → _collect_coding_mode_tools()  # LSP/AST 工具
    → _collect_driver_tools()       # MCP/Driver 外部工具
    → memory_tools                  # 记忆工具
  → Toolkit → QwenPawAgent
```

### 2.2 当前工作区模型（无用户隔离）

```
WORKING_DIR (~/.qwenpaw/)
├── workspaces/
│   └── default/          ← 所有用户共享同一个工作区
│       ├── agent.json
│       ├── chats.json    ← 所有用户的对话记录混在一起
│       ├── sessions/     ← 仅按文件名分区 <user>_<sid>.json
│       ├── memory/       ← 共享
│       └── credentials/  ← 共享
└── config.json
```

**关键问题**：
- `Workspace` 按 `agent_id` 创建，不感知 `user_id`
- `ChannelManager` 绑定到单个 `Workspace`，所有用户消息进入同一工作区
- `chats.json` 中所有用户的对话记录混存，仅靠 `user_id` 字段过滤
- 一个用户可通过会话 API 读取其他用户的对话历史

### 2.3 现有安全层（Gateway 和隔离不替代它们）

| 层级 | 组件 | 职责 |
|------|------|------|
| 工具选择 | `ToolRegistry.filter()` | 构造时决定哪些工具可用 |
| 治理策略 | `PolicyGuardedTool` + `ResourceGovernor` | 运行时按调用参数决策 |
| 驱动策略 | `DriverPolicy` | MCP 外部工具访问控制 |
| 安全扫描 | `ToolGuardEngine` | 执行前参数扫描 |

---

## 三、Tool Gateway：MCP 协议设计

### 3.1 协议选择

**使用 MCP 协议**。理由：
- 标准化的 `tools/list` 接口天然适配"返回可用工具列表"的场景
- MCP 的 `initialize` 阶段可传递 `user_id` 等上下文
- Gateway 既能做授权（返回内部工具名），也能提供自定义工具（Gateway 托管执行）
- 与 QwenPaw 现有的 MCP 基础设施（DriverManager、MCPDriverHandler）可复用

### 3.2 交互流程

```
用户发送消息
  │
  ▼
AgentBuilder.build()
  │
  ├─ 提取 user_id, channel, session_id
  │
  ├─ ToolGatewayService.fetch_authorized_tools(user_id, channel)
  │    │
  │    ├─ 建立 MCP 连接 (HTTP/SSE transport)
  │    ├─ initialize({user_id: "alice", channel: "discord", ...})
  │    ├─ tools/list()
  │    │   返回: [{name: "read_file", ...}, {name: "grep_search", ...}]
  │    └─ 缓存结果 (TTL 60s)
  │
  ├─ 解析 → authorized_tool_names = {"read_file", "grep_search", ...}
  │
  ├─ 分离两类工具:
  │    ├─ 内部工具: 名称匹配 ToolRegistry → allowed 集合
  │    └─ 外部工具: 不匹配 → GatewayCapabilityTool
  │
  └─ build_toolkit(allowed=authorized_internal_names, gateway_tools=external_tools)
```

### 3.3 MCP 消息约定

**Initialize 请求**：
```json
{
  "method": "initialize",
  "params": {
    "protocolVersion": "2024-11-05",
    "clientInfo": {"name": "qwenpaw-tool-gateway", "version": "1.0"},
    "user_id": "alice",
    "channel": "discord",
    "session_id": "discord:alice"
  }
}
```

**tools/list 响应**（仅返回授权工具）：
```json
{
  "tools": [
    {
      "name": "read_file",
      "description": "Read a file from the local filesystem",
      "inputSchema": { ... }
    },
    {
      "name": "grep_search",
      "description": "Search file contents with regex",
      "inputSchema": { ... }
    },
    {
      "name": "gateway_custom_search",
      "description": "Search internal knowledge base",
      "inputSchema": { ... }
    }
  ]
}
```

**约定**：
- 工具名 `read_file`、`grep_search` 等匹配内部 `ToolDescriptor.name` → 映射到内置工具
- 工具名不匹配任何内部工具 → 作为 Gateway 托管的外部工具
- Gateway 对每个用户返回不同列表，实现用户级授权

---

## 四、用户工作区隔离设计

### 4.1 目标隔离模型

```
WORKING_DIR (~/.qwenpaw/)
├── users/
│   ├── alice/
│   │   └── workspaces/
│   │       └── default/
│   │           ├── agent.json
│   │           ├── chats.json     ← 仅 alice 的对话
│   │           ├── sessions/      ← 仅 alice 的会话
│   │           ├── memory/        ← 仅 alice 的记忆
│   │           ├── credentials/   ← 仅 alice 的凭证
│   │           ├── skills/        ← 仅 alice 的技能
│   │           └── coding_projects/
│   ├── bob/
│   │   └── workspaces/
│   │       └── default/
│   │           └── ...            ← bob 完全不可见 alice 的数据
│   └── _templates/
│       └── default/               ← 新用户自动从模板克隆
│           └── agent.json
└── config.json                    ← 全局配置（共享）
```

**核心原则**：文件系统级别隔离，`user_id` 直接对应一个目录子树。

### 4.2 路由变更：ChannelManager 用户感知

**现状**：
```
ChannelManager ──绑定──→ Workspace("default")
  所有 user_id 的消息都进入同一个工作区
```

**目标**：
```
ChannelManager ──路由──→ UserWorkspaceResolver
                          ├─ alice → Workspace("alice/default")
                          └─ bob   → Workspace("bob/default")
  每个 user_id 的消息进入独立工作区
```

**改动点**：

#### a) `MultiAgentManager` 增加用户维度

```python
# src/qwenpaw/app/multi_agent_manager.py

class MultiAgentManager:
    def __init__(self, ...):
        self._agents: dict[str, Workspace] = {}              # 现有
        self._user_agents: dict[tuple[str, str], Workspace] = {}  # 新增: (user_id, agent_id)

    async def get_agent_for_user(self, agent_id: str, user_id: str) -> Workspace:
        """获取或创建用户专属的工作区。"""
        key = (user_id, agent_id)
        if key in self._user_agents:
            return self._user_agents[key]

        user_workspace_dir = self._resolve_user_workspace_dir(agent_id, user_id)
        workspace = await self._create_or_clone_user_workspace(
            agent_id, user_id, user_workspace_dir
        )
        self._user_agents[key] = workspace
        return workspace

    def _resolve_user_workspace_dir(self, agent_id: str, user_id: str) -> Path:
        return WORKING_DIR / "users" / _sanitize(user_id) / "workspaces" / agent_id
```

#### b) `ChannelManager._consume_with_tracker` — 按用户路由

```python
# src/qwenpaw/app/channels/manager.py

async def _consume_with_tracker(self, ch, request, payload):
    user_id = request.user_id or "anonymous"
    agent_id = self._resolve_agent_id(request)

    # 获取用户专属工作区
    workspace = await self._user_workspace_resolver.get_workspace(agent_id, user_id)

    # 后续流程使用用户专属工作区
    await workspace.stream_query(request)
```

#### c) `Workspace` 构造 — 自动克隆模板

```python
# 新用户首次消息 → 自动创建工作区
async def _create_or_clone_user_workspace(agent_id, user_id, user_workspace_dir):
    if not user_workspace_dir.exists():
        template_dir = WORKING_DIR / "users" / "_templates" / agent_id
        if template_dir.exists():
            shutil.copytree(template_dir, user_workspace_dir)
        else:
            # 从默认工作区克隆
            default_dir = WORKING_DIR / "workspaces" / agent_id
            shutil.copytree(default_dir, user_workspace_dir)

    workspace = Workspace(f"{agent_id}:{user_id}", user_workspace_dir)
    await workspace.start()
    return workspace
```

### 4.3 隔离影响范围

| 数据 | 隔离方式 | 变更 |
|------|---------|------|
| 会话 (sessions) | 目录级别隔离 | 文件名不再需要 `user_id` 前缀 |
| 对话 (chats.json) | 每用户独立文件 | 不再需要跨用户过滤 |
| 记忆 (memory) | 每用户独立目录 | 天然隔离 |
| 凭证 (credentials) | 每用户独立 keyring | 天然隔离 |
| 技能 (skills) | 每用户独立目录 | 可复用模板克隆 |
| 文件操作 (read/write) | 工具 guard 限制到工作区 | 已在 `file_guardian.py` 中实现 |
| Shell 执行 | 工具 guard 限制 | 已在 `shell_evasion_guardian.py` 中 |
| 定时任务 (cron) | 每用户独立 jobs.json | 天然隔离 |
| 插件 (plugins) | 每用户独立目录 | 可配置是否共享 |

### 4.4 ChannelManager 与 Workspace 的绑定变更

**关键架构决策**：`ChannelManager` 不再 1:1 绑定到 Workspace。

```
现状:  ChannelManager → Workspace("default")        (1:1 绑定)
目标:  ChannelManager → UserWorkspaceResolver        (1:N 路由)
                          ├─ get_workspace(agent_id, user_id)
                          └─ 缓存 {(agent_id, user_id): Workspace}
```

`ChannelManager` 持有 `UserWorkspaceResolver` 引用，每次消费消息时根据 `user_id` 动态解析目标工作区。

### 4.5 向后兼容

| 场景 | 行为 |
|------|------|
| 配置未启用用户隔离 | ChannelManager 直接使用 Workspace（现状） |
| 用户隔离启用 + 已有用户 | 按 user_id 路由到对应子目录 |
| 用户隔离启用 + 新用户 | 自动从 `_templates/default/` 克隆工作区 |
| 无 user_id（匿名请求） | 路由到 `users/anonymous/` 或拒绝 |

**配置开关**（`config.json`）：

```json
{
  "multi_tenant": {
    "enabled": true,
    "user_workspace_base": "users",
    "auto_create_user_workspace": true,
    "template_agent_id": "default"
  }
}
```

---

## 五、Tool Gateway 与用户隔离的协作

两者协同工作，形成完整的用户级安全边界：

```
用户消息抵达
  │
  ├─ 1. user_id 提取
  │
  ├─ 2. 工作区路由 (user_id → Workspace)
  │     └─ 创建/获取用户专属工作区目录
  │
  ├─ 3. Tool Gateway 授权 (user_id → 可用工具列表)
  │     └─ MCP tools/list，返回授权工具名
  │
  ├─ 4. 工具组装 (仅授权工具 + 仅用户工作区上下文)
  │     └─ file_search 只搜用户目录
  │     └─ session 只读用户会话
  │
  └─ 5. 执行 (PolicyGuardedTool + ToolGuard)
```

**协作要点**：
- Tool Gateway 的 `user_id` 即工作区隔离的 `user_id`，两者使用同一身份
- Gateway 返回的工具列表中，`read_file`/`write_file` 等路径操作工具自动受限于用户工作区
- Gateway 同时充当统一授权入口：Gateway 不授权的工具，用户工作区中也不可用

---

## 六、新增组件设计

### 6.1 `ToolGatewayService`

**位置**: `src/qwenpaw/app/tool_gateway/service.py`

### 6.2 `GatewayCapabilityTool`

**位置**: `src/qwenpaw/app/tool_gateway/capability_tool.py`

### 6.3 `UserWorkspaceResolver`

**位置**: `src/qwenpaw/app/workspace/user_resolver.py`

```python
class UserWorkspaceResolver:
    """按 user_id 路由到独立工作区。"""

    def __init__(self, manager: MultiAgentManager, config: dict):
        self._manager = manager
        self._cache: dict[tuple[str, str], Workspace] = {}

    async def get_workspace(self, agent_id: str, user_id: str) -> Workspace:
        key = (agent_id, user_id)
        if key not in self._cache:
            self._cache[key] = await self._manager.get_agent_for_user(
                agent_id, user_id
            )
        return self._cache[key]
```

### 6.4 配置

```json
{
  "tools": {
    "tool_gateway": {
      "enabled": true,
      "url": "http://tool-gateway.internal/mcp",
      "transport": "streamable_http",
      "cache_ttl_seconds": 60,
      "timeout_seconds": 5
    }
  },
  "multi_tenant": {
    "enabled": true,
    "user_workspace_base": "users",
    "auto_create_user_workspace": true,
    "template_agent_id": "default"
  }
}
```

### 6.5 `AgentBuilder.build_toolkit()` 改动

```python
async def build_toolkit(self, ...):
    # === Tool Gateway check ===
    gateway_service = get_gateway_service(ctx)
    if gateway_service and gateway_service.enabled:
        authorized_tools = await gateway_service.fetch_authorized_tools(
            user_id=request_context.get("user_id", ""),
            channel=request_context.get("channel", ""),
        )
        if not authorized_tools:
            return Toolkit(tools=[])

    internal_names = _resolve_internal_tool_names(authorized_tools)
    allowed = set(internal_names) if gateway_service else None

    tools = await local_ws.list_tools(..., allowed=allowed, ...)

    # 外部 gateway 工具
    external_tool_defs = _resolve_external_tool_defs(authorized_tools)
    gateway_tools = [
        GatewayCapabilityTool(td, gateway_service, request_context)
        for td in external_tool_defs
    ]
    tools.extend(gateway_tools)
    ...
```

---

## 七、安全与容错

### 7.1 Fail-Closed 策略

| 场景 | 行为 |
|------|------|
| 多租户未启用 | 正常流程（向后兼容） |
| 多租户启用 + 用户无工作区 | 自动从模板克隆创建 |
| Gateway 未启用 | 正常流程 |
| Gateway 已启用但不可达 | **返回空工具列表**——Agent 无工具可用 |
| Gateway 返回空列表 | 同上 |
| Gateway 超时 | 同上（超时时间: 5s） |

### 7.2 缓存

- Tool Gateway：按 `user_id` 缓存授权工具列表，TTL 60s
- 用户工作区：按 `(agent_id, user_id)` 缓存 Workspace 实例，跟随 Workspace 生命周期

### 7.3 安全层次

```
用户工作区隔离 (文件系统级)
  ↓
Tool Gateway (user 级工具授权)
  ↓
ToolRegistry.filter() (工具选择)
  ↓
PolicyGuardedTool.check_permissions() (治理策略，运行时)
  ↓
ToolGuardEngine.guard() (安全扫描)
  ↓
执行
```

---

## 八、Gateway 服务器参考实现

```python
from mcp.server import Server
from mcp.types import Tool

USER_TOOLS = {
    "alice": ["read_file", "write_file", "grep_search", "execute_shell_command"],
    "bob":   ["read_file", "grep_search"],
}

async def serve(user_id: str):
    tools = USER_TOOLS.get(user_id, [])
    return [
        Tool(name=t, description=f"Built-in: {t}", inputSchema={...})
        for t in tools
    ]
```

---

## 九、实现步骤

| 步骤 | 内容 | 涉及文件 |
|------|------|----------|
| 1 | 新增 `UserWorkspaceResolver` | `src/qwenpaw/app/workspace/user_resolver.py` |
| 2 | 新增 `MultiAgentManager.get_agent_for_user()` | `src/qwenpaw/app/multi_agent_manager.py` |
| 3 | 新增工作区自动克隆逻辑 | `src/qwenpaw/app/workspace/user_resolver.py` |
| 4 | 修改 `ChannelManager` 支持用户路由 | `src/qwenpaw/app/channels/manager.py` |
| 5 | 修改 `service_factories.create_channel_service` | `src/qwenpaw/app/workspace/service_factories.py` |
| 6 | 新增多租户配置 schema | `src/qwenpaw/config/` |
| 7 | 新增 `ToolGatewayService` | `src/qwenpaw/app/tool_gateway/service.py` |
| 8 | 新增 `GatewayCapabilityTool` | `src/qwenpaw/app/tool_gateway/capability_tool.py` |
| 9 | 修改 `AgentBuilder.build_toolkit()` | `src/qwenpaw/runtime/builder.py` |
| 10 | 修改 `QwenPawLocalWorkspace.list_tools()` | `src/qwenpaw/app/workspace/local_workspace.py` |
| 11 | 编写单元测试 | `tests/` |
| 12 | 编写 Gateway 参考实现 | 可选 |

---

## 十、关键设计决策

| 决策 | 选择 | 理由 |
|------|------|------|
| Gateway 协议 | MCP | 标准化、复用现有基础设施、生态兼容 |
| Gateway 插入点 | `AgentBuilder.build_toolkit()` | 唯一汇聚点 |
| 用户隔离粒度 | 文件系统目录级 | 最强隔离、操作简单、审计友好 |
| 工作区路由 | `user_id` → Workspace 目录 | 直接映射，无需额外权限表 |
| 新用户工作区 | 自动从模板克隆 | 零配置、即时可用 |
| ChannelManager 绑定 | 1:N 动态路由 | 一个通道服务所有用户 |
| 失败策略 | Fail-closed | 安全优先 |
| 向后兼容 | 开关控制 | 不破坏现有单租户部署 |
