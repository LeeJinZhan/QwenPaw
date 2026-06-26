# Tool Gateway 设计方案

## 一、需求概述

在 QwenPaw 中增加 Tool Gateway 功能：
- 基于 `user_id` 从 Gateway 端点获取该用户可用的工具列表
- **包括内置工具在内的所有工具**，未获 Gateway 授权的均不可使用
- Gateway 端点基于 MCP 协议设计

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

### 2.2 关键切入点

`AgentBuilder.build_toolkit()` 是唯一组装所有工具的汇聚点。在此处插入 Gateway 查询，将授权工具列表作为 `allowed` 集合传给 `ToolRegistry.filter()`，可一次性过滤所有内置/技能/插件工具。

### 2.3 现有安全层（Gateway 不替代它们）

| 层级 | 组件 | 职责 |
|------|------|------|
| 工具选择 | `ToolRegistry.filter()` | 构造时决定哪些工具可用 |
| 治理策略 | `PolicyGuardedTool` + `ResourceGovernor` | 运行时按调用参数决策 |
| 驱动策略 | `DriverPolicy` | MCP 外部工具访问控制 |
| 安全扫描 | `ToolGuardEngine` | 执行前参数扫描 |

**Tool Gateway 工作在"工具选择"层**——它决定哪些工具名称可以进入 Toolkit，不替代运行时治理/策略/扫描。

---

## 三、核心设计：MCP 协议驱动的 Tool Gateway

### 3.1 协议选择

**推荐使用 MCP 协议**。理由：
- 标准化的 `tools/list` 接口天然适配"返回可用工具列表"的场景
- MCP 的 `initialize` 阶段可传递 `user_id` 等上下文
- Gateway 既能做授权（返回内部工具名），也能提供自定义工具（Gateway 托管执行）
- 与 QwenPaw 现有的 MCP 基础设施（DriverManager、MCPDriverHandler）可复用
- 生态兼容——任何标准 MCP Server 都可作为 Gateway

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
  │    │   返回: [{name: "read_file", description: ..., inputSchema: ...},
  │    │          {name: "grep_search", ...},
  │    │          {name: "my_custom_tool", ...}]   ← 仅授权工具
  │    └─ 缓存结果 (TTL 60s)
  │
  ├─ 解析返回的工具名列表 → authorized_tool_names = {"read_file", "grep_search", ...}
  │
  ├─ 分离两类工具:
  │    ├─ 内部工具: 名称匹配 ToolRegistry 中已注册的 ToolDescriptor
  │    │   → 传入 allowed=authorized_internal_names
  │    └─ 外部工具: 名称不在 ToolRegistry 中
  │        → 创建 GatewayCapabilityTool (Gateway 负责执行)
  │
  ├─ build_toolkit(allowed=authorized_internal_names, gateway_tools=external_tools)
  │    │
  │    ├─ local_ws.list_tools(allowed=authorized_internal_names)
  │    │   → ToolRegistry.filter(allowed=authorized_internal_names, denied=...)
  │    │   → 仅返回 Gateway 授权的内置工具
  │    │
  │    ├─ coding_mode_tools 同理按 allowed 过滤
  │    ├─ gateway_tools 作为 extra_tools 注入
  │    └─ 返回 Toolkit
  │
  └─ QwenPawAgent(toolkit)
```

### 3.3 MCP 消息约定

#### Initialize 请求

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

#### tools/list 响应（仅返回授权工具）

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
- 工具名 `read_file`、`grep_search` 等匹配 QwenPaw 内部 `ToolDescriptor.name` → 映射到内置工具
- 工具名 `gateway_*` 等不匹配任何内部工具 → 作为 Gateway 托管的外部工具
- Gateway 的 `tools/list` 对每个用户返回不同列表，实现用户级授权

---

## 四、新增组件设计

### 4.1 `ToolGatewayService`

**位置**: `src/qwenpaw/app/tool_gateway/service.py`

```python
class ToolGatewayService:
    """Manage Tool Gateway MCP connection and fetch authorized tools per user."""

    def __init__(self, gateway_url: str, cache_ttl: int = 60):
        self._gateway_url = gateway_url
        self._cache: dict[str, tuple[float, list[dict]]] = {}
        self._cache_ttl = cache_ttl

    async def fetch_authorized_tools(
        self,
        user_id: str,
        channel: str = "",
        session_id: str = "",
    ) -> list[dict]:
        """Return the list of tool definitions authorized for this user.

        Each tool dict has: name, description, inputSchema (JSON Schema).
        Returns empty list if gateway is unreachable (fail-closed).
        """
        # 1. Check cache
        cached = self._cache.get(user_id)
        if cached and time.time() < cached[0]:
            return cached[1]

        # 2. Connect to gateway MCP server
        try:
            async with self._connect(user_id, channel, session_id) as session:
                tools = await session.list_tools()
        except Exception:
            logger.error(f"Tool gateway unreachable for user {user_id}")
            return []

        # 3. Cache and return
        self._cache[user_id] = (time.time() + self._cache_ttl, tools)
        return tools

    async def execute_gateway_tool(
        self,
        user_id: str,
        tool_name: str,
        arguments: dict,
    ) -> dict:
        """Execute a gateway-hosted tool."""
        ...
```

### 4.2 `GatewayCapabilityTool`

**位置**: `src/qwenpaw/app/tool_gateway/capability_tool.py`

复用 `DriverCapabilityTool` 的模式，为 Gateway 托管的外部工具创建 AgentScope `ToolBase` 适配器：

```python
class GatewayCapabilityTool(ToolBase):
    """Wraps a gateway-hosted tool as an AgentScope tool."""

    def __init__(self, tool_def: dict, gateway_service: ToolGatewayService,
                 request_context: dict):
        self._tool_def = tool_def
        self._gateway = gateway_service
        self._request_context = request_context

    async def __call__(self, **kwargs) -> ToolChunk:
        user_id = self._request_context["user_id"]
        result = await self._gateway.execute_gateway_tool(
            user_id, self.name, kwargs
        )
        return ToolChunk(content=result)
```

### 4.3 `AgentBuilder` 改动

**位置**: `src/qwenpaw/runtime/builder.py`

改动集中在 `build_toolkit()` 方法：

```python
async def build_toolkit(self, ...):
    # === NEW: Tool Gateway check ===
    gateway_service = get_gateway_service(ctx)
    authorized_tools = []
    if gateway_service and gateway_service.enabled:
        authorized_tools = await gateway_service.fetch_authorized_tools(
            user_id=request_context.get("user_id", ""),
            channel=request_context.get("channel", ""),
            session_id=request_context.get("session_id", ""),
        )
        if not authorized_tools:
            return Toolkit(tools=[])

    # 分离内部/外部工具
    internal_names = _resolve_internal_tool_names(authorized_tools)
    external_tool_defs = _resolve_external_tool_defs(authorized_tools)

    # 向后兼容：无 gateway 时 allowed=None（走原流程）
    allowed = set(internal_names) if gateway_service else None

    # 内置工具过滤
    tools = await local_ws.list_tools(..., allowed=allowed, ...)

    # 外部 gateway 工具
    gateway_tools = [
        GatewayCapabilityTool(td, gateway_service, request_context)
        for td in external_tool_defs
    ]
    tools.extend(gateway_tools)

    # coding/driver/memory 工具同样受 allowed 过滤
    ...
```

### 4.4 配置

**Agent 配置新增字段**（`agent_config.tools.tool_gateway`）:

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
  }
}
```

### 4.5 `ToolRegistry.filter()` 改动

`allowed` 参数已存在且逻辑正确（非空时仅允许列表内工具）。**无需改动**。

`QwenPawLocalWorkspace.list_tools()` 需小幅修改：当外部传入 `allowed` 时，不覆盖为 config 计算的 allowed。

---

## 五、Gateway 服务器参考实现

Gateway 本身是一个标准 MCP Server，示例（Python）：

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

Gateway 可实现为：
- 独立 MCP Server（与 QwenPaw 解耦）
- 对接企业 IAM/权限系统
- 为不同用户返回不同工具集
- 也可提供 Gateway 自有的自定义工具

---

## 六、安全与容错

### 6.1 Fail-Closed 策略

| 场景 | 行为 |
|------|------|
| Gateway 未配置/未启用 | 正常流程（向后兼容） |
| Gateway 已启用但不可达 | **返回空工具列表**——Agent 无工具可用 |
| Gateway 返回空列表 | 同上 |
| Gateway 超时 | 同上（超时时间: 5s） |
| Gateway 返回未知工具名 | 忽略，仅匹配已知内部工具 |

### 6.2 缓存

- 按 `user_id` 缓存授权工具列表
- 默认 TTL 60 秒（可配）
- 避免每次对话请求都查询 Gateway
- 缓存不跨进程共享（每 worker 独立缓存）

### 6.3 与现有安全层的关系

```
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

**Gateway 是第一关**——决定"用户能看见哪些工具"。后续关卡继续有效。

---

## 七、实现步骤

| 步骤 | 内容 | 涉及文件 |
|------|------|----------|
| 1 | 新增 `ToolGatewayService` | `src/qwenpaw/app/tool_gateway/__init__.py`, `service.py` |
| 2 | 新增 `GatewayCapabilityTool` | `src/qwenpaw/app/tool_gateway/capability_tool.py` |
| 3 | 新增 Gateway 配置 schema | `src/qwenpaw/config/` |
| 4 | 修改 `AgentBuilder.build_toolkit()` | `src/qwenpaw/runtime/builder.py` |
| 5 | 修改 `QwenPawLocalWorkspace.list_tools()` | `src/qwenpaw/app/workspace/local_workspace.py` |
| 6 | 修改 `_collect_coding_mode_tools()` / `_collect_driver_tools()` | `src/qwenpaw/runtime/builder.py` |
| 7 | 编写单元测试 | `tests/` |
| 8 | 编写 Gateway 参考实现 | 可选，可用 `scripts/` 或独立仓库 |

---

## 八、关键设计决策

| 决策 | 选择 | 理由 |
|------|------|------|
| 协议 | MCP | 标准化、可复用现有基础设施、生态兼容 |
| 插入点 | `AgentBuilder.build_toolkit()` | 唯一汇聚点，一次性过滤所有工具 |
| 失败策略 | Fail-closed (空工具列表) | 安全优先 |
| 缓存 | per-user, 60s TTL | 平衡延迟与一致性 |
| 工具分类 | 内部名匹配 vs 外部托管 | Gateway 既可授权又可提供自定义工具 |
| 向后兼容 | Gateway 未配置时走原流程 | 不破坏现有用户 |
