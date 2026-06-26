# QwenPaw 多场景多用户 Composite Workspace 设计方案

> **版本**: v1.0  
> **日期**: 2026-06-26  
> **状态**: 方案设计阶段

---

## 目录

1. [背景与需求](#1-背景与需求)
2. [现有架构分析](#2-现有架构分析)
3. [方案演进](#3-方案演进)
4. [最终方案：存储抽象层 + 内存合并](#4-最终方案存储抽象层--内存合并)
5. [核心接口设计](#5-核心接口设计)
6. [实现详解](#6-实现详解)
7. [配置与部署](#7-配置与部署)
8. [附录](#8-附录)

---

## 1. 背景与需求

### 1.1 问题描述

QwenPaw 原生架构中，每个 Agent 绑定一个 workspace 目录。在实际业务场景中，需要将 workspace 按两个正交维度拆解：

- **场景维度**：智能问数、智能文答、知识库检索等不同业务场景，各自有独立的 prompt 定义、技能工具包
- **用户维度**：用户 a、b、c、d 各自有独立的身份档案、偏好记忆、个性化技能

每次请求需要**同时加载场景 workspace 和用户 workspace**，合并为一个完整的 Agent 运行上下文。

### 1.2 设计目标

| 目标 | 说明 |
|------|------|
| 正交组合 | 场景 × 用户独立管理，请求时动态组合 |
| 零额外 IO | 不创建临时目录、不复制文件，纯内存合并 |
| 存储无关 | 支持文件系统和数据库等多种存储后端，可按 workspace 粒度独立配置 |
| 零内核侵入 | 不修改 QwenPaw 现有 Agent/PromptManager/ToolRegistry 代码 |
| 向后兼容 | 不传 scene/user_id 时完全走原逻辑 |

---

## 2. 现有架构分析

### 2.1 QwenPaw Workspace 加载机制

QwenPaw 的 workspace 在每次请求时通过 `AgentBuilder.build(ctx)` 加载，核心调用链如下：

```
AgentBuilder.build(ctx)
    │
    ├── ensure_skills_initialized(workspace_dir)
    │       └── reconcile_workspace_manifest(workspace_dir)
    │               └── 扫描 {workspace_dir}/skills/ → 写入 skill.json
    │
    ├── resolve_effective_skills(workspace_dir, channel_name)
    │       └── 读 skill.json，过滤 enabled + channel → 返回技能名列表
    │
    ├── QwenPawLocalWorkspace.list_tools()
    │       └── 遍历 effective_skills，读每个 {workspace_dir}/skills/{name}/SKILL.md
    │
    └── build_prompt(ctx, agent_config)
            └── PromptManager.build_sync(ctx)
                    └── 8 个 PromptContributor 按优先级依次执行：
                        ├── AgentIdentityContributor     (priority=5)
                        ├── AgentsMdContributor         (priority=10)   ← 读 AGENTS.md
                        ├── SoulMdContributor           (priority=20)   ← 读 SOUL.md
                        ├── ProfileMdContributor        (priority=30)   ← 读 PROFILE.md
                        ├── MultimodalHintContributor   (priority=80)
                        ├── CodingModeContributor       (priority=85)
                        ├── DriverPolicyHintContributor (priority=88)
                        └── EnvContextContributor       (priority=90)
```

### 2.2 关键发现

1. **所有资源加载都基于 `workspace_dir`**：prompt 文件、skills 目录、skill.json 清单均从此路径读取
2. **PromptManager 使用 Contributor 模式**：每个 Contributor 是独立的生产函数，按 priority 排序拼接
3. **Skills 通过 manifest 管理**：`skill.json` 是 skills 的运行时清单，记录 enabled/channels/config
4. **Tool 构造与 skill 目录紧密耦合**：`list_tools()` 直接遍历文件系统

### 2.3 文件归属语义

| 文件 | 语义 | 归属 |
|------|------|------|
| `SOUL.md` | Agent 核心人格、价值观、行为约束 | 场景 |
| `AGENTS.md` | 多 Agent 协作规则、工作流 SOP | 场景为主 |
| `PROFILE.md` | 用户身份、偏好、背景 | 用户 |
| `MEMORY.md` | 长期记忆入口 | 用户 |
| `HEARTBEAT.md` | 周期性维护任务 | 用户 |
| `skills/` | 技能工具包 | 场景 + 用户 |
| `skill.json` | 技能清单（enabled/channels/config） | 场景 + 用户 |

---

## 3. 方案演进

### 3.1 方案一：磁盘合并（已废弃）

**思路**：每次请求创建临时目录，将两个 workspace 的文件复制合并到临时目录，覆盖 `ctx.workspace_dir`。

**问题**：
- 每次请求创建临时目录 + 复制文件 + 删除目录，IO 开销大
- 并发请求存在清理竞争
- 临时目录残留风险

### 3.2 方案二：内存合并 Prompt（部分覆盖）

**思路**：在 `AgentBuilder.build_prompt()` 中直接读两个目录的 prompt 文件，内存中拼接。

**问题**：
- 只覆盖了 prompt 文件，skills 走原逻辑
- 无法做到存储无关

### 3.3 最终方案：存储抽象层 + 全内存合并

**核心思路**：引入三层抽象

```
┌──────────────────────────────────────────────┐
│            CompositeWorkspaceResolver         │  ← 业务层：合并逻辑
│        (prompt 策略 + skill 去重合并)          │
│                                               │
│  ┌─────────────┐    ┌──────────────────────┐  │
│  │ SceneStorage │    │    UserStorage       │  │  ← 存储抽象层
│  │ (可以是 FS)  │    │ (可以是 DB/FS/...)   │  │
│  └─────────────┘    └──────────────────────┘  │
│                                               │
│     WorkspaceStorage 接口                      │  ← 统一接口
│     ├── read_prompt(filename) → PromptFile    │
│     └── list_skill_entries(channel) → [Skill] │
└──────────────────────────────────────────────┘
```

---

## 4. 最终方案：存储抽象层 + 内存合并

### 4.1 总体架构

```
请求 {"scene":"smart_query", "user_id":"a"}
         │
         ▼
   AgentBuilder.build(ctx)
         │
   ┌─────┴─────┐
   │ scene &&  │  NO → 走原有单 workspace 逻辑
   │ user_id ? │
   └─────┬─────┘
         │ YES
         ▼
   _build_composite(ctx, scene, user_id)
         │
         ├── ① _get_workspace_config("scenes", scene)  → 查配置
         ├── ② _get_workspace_config("users", user_id)
         │
         ├── ③ create_workspace_storage(scene_config)  → SceneStorage
         ├── ④ create_workspace_storage(user_config)    → UserStorage
         │                                           (可以是 FS / DB / 混合)
         │
         ├── ⑤ CompositeWorkspaceResolver(scene_storage, user_storage)
         │       ├── build_workspace_section() → 内存合并 prompt
         │       └── resolve_skills(channel)   → 内存合并 skills
         │
         ├── ⑥ 组装 system_prompt = ws_section + env_context + ...
         ├── ⑦ 组装 toolkit = _build_toolkit_from_skills(effective_skills)
         └── ⑧ 构造 QwenPawAgent(system_prompt, toolkit, model)
```

### 4.2 存储抽象层架构

```
        ┌──────────────────────────────┐
        │     WorkspaceStorage (ABC)   │
        ├──────────────────────────────┤
        │ + storage_type: str          │
        │ + read_prompt(name)          │
        │ + read_prompts_batch(names)  │
        │ + list_skill_entries(ch)     │
        │ + read_skill_content(name)   │
        │ + read_manifest()            │
        └──────────┬───────────────────┘
                   │
     ┌─────────────┼─────────────┐
     │             │             │
     ▼             ▼             ▼
┌─────────┐ ┌───────────┐ ┌──────────┐
│ FsStorage│ │ DbStorage │ │ S3Storage│  (可扩展)
│ (文件)   │ │ (数据库)  │ │ (对象)   │
└─────────┘ └───────────┘ └──────────┘
```

### 4.3 数据流（无磁盘写入）

```
CompositeWorkspaceResolver
    │
    ├─ build_workspace_section()
    │       │
    │       ├─ scene.read_prompt("SOUL.md")      → PromptFile(内存)
    │       ├─ user.read_prompt("PROFILE.md")     → PromptFile(内存)
    │       ├─ scene.read_prompt("AGENTS.md")     → PromptFile(内存)
    │       └─ user.read_prompt("AGENTS.md")      → PromptFile(内存)
    │       │
    │       └─ 按策略合并字符串 → str
    │
    └─ resolve_skills(channel)
            │
            ├─ scene.list_skill_entries(channel)  → [SkillEntry](内存)
            ├─ user.list_skill_entries(channel)   → [SkillEntry](内存)
            │
            └─ 去重（同名用户覆盖场景） → [SkillEntry]

全程零磁盘写入，不创建任何临时文件。
```

### 4.4 文件合并策略

| 文件 | 主来源 | 次来源 | 策略 |
|------|--------|--------|------|
| `SOUL.md` | Scene | — | 场景独占 |
| `AGENTS.md` | Scene | User | 场景 base + 用户 overlay 追加 |
| `PROFILE.md` | User | — | 用户独占 |
| `MEMORY.md` | User | — | 用户独占 |
| `HEARTBEAT.md` | User | — | 用户独占 |
| `SCENE_CONFIG.md` | Scene | — | 场景独占 |

### 4.5 技能合并策略

- 两个 workspace 的 skills 按 `name` 去重
- **同名技能**：用户 workspace 的技能覆盖场景同名技能
- **独有技能**：各自保留
- `enabled`、`channels` 过滤在 `list_skill_entries()` 层面完成

---

## 5. 核心接口设计

### 5.1 WorkspaceStorage（抽象基类）

```python
class WorkspaceStorage(ABC):
    """存储后端抽象接口。"""

    storage_type: str = "abstract"

    # === Prompt 文件操作 ===

    @abstractmethod
    def read_prompt(self, filename: str) -> Optional[PromptFile]:
        """读取 prompt 文件，不存在返回 None。"""

    def read_prompts_batch(self, filenames: list[str]) -> dict[str, PromptFile]:
        """批量读取，默认逐个调用，子类可覆盖（如 SQL IN 查询）。"""

    # === Skills 操作 ===

    @abstractmethod
    def list_skill_entries(self, channel: str = "all") -> list[SkillEntry]:
        """获取指定渠道下所有启用的技能。"""

    @abstractmethod
    def read_skill_content(self, skill_name: str) -> Optional[str]:
        """读取单个技能的 SKILL.md 全文。"""

    # === Manifest ===

    def read_manifest(self) -> dict:
        """读取技能清单元信息。"""
```

### 5.2 数据类

```python
@dataclass
class PromptFile:
    name: str                    # 文件名（如 SOUL.md）
    content: str                 # 正文（已剥离 YAML frontmatter）
    raw_content: str             # 原始内容
    frontmatter: dict | None     # YAML frontmatter 解析结果

@dataclass
class SkillEntry:
    name: str                    # 技能名
    content: str                 # SKILL.md 全文
    enabled: bool = True
    channels: list[str] | None   # None = all
    config: dict | None = None
    metadata: dict | None = None
```

### 5.3 CompositeWorkspaceResolver

```python
class CompositeWorkspaceResolver:
    """场景 Workspace + 用户 Workspace 的内存合并器。"""

    FILES = {
        "SOUL.md":     {"primary": "scene", "strategy": "primary"},
        "AGENTS.md":   {"primary": "scene", "secondary": "user",
                        "strategy": "scene_base_user_overlay"},
        "PROFILE.md":  {"primary": "user",  "strategy": "primary"},
        "MEMORY.md":   {"primary": "user",  "strategy": "primary"},
    }

    def __init__(self, scene_storage: WorkspaceStorage,
                 user_storage: WorkspaceStorage):
        ...

    def build_workspace_section(self) -> str:
        """合并 prompt 文件为 system prompt 的 workspace 段落。"""

    def resolve_skills(self, channel: str = "all") -> list[SkillEntry]:
        """合并去重 skills 列表。"""
```

### 5.4 工厂函数

```python
def create_workspace_storage(config: dict) -> WorkspaceStorage:
    """
    根据配置创建存储后端。

    config 示例:
      {"type": "filesystem", "dir": "~/.qwenpaw/scenes/smart_query"}
      {"type": "database", "dsn": "sqlite:///qwenpaw.db", "workspace_id": "user_a"}
    """

def register_storage(type_name: str, klass: type[WorkspaceStorage]):
    """注册自定义存储后端。"""
```

---

## 6. 实现详解

### 6.1 文件系统实现（`FsWorkspaceStorage`）

```
存储路径:
  {workspace_dir}/
  ├── SOUL.md
  ├── AGENTS.md
  ├── PROFILE.md
  ├── MEMORY.md
  ├── skill.json          ← 技能清单
  └── skills/
      ├── skill_a/
      │   └── SKILL.md
      └── skill_b/
          └── SKILL.md

特点:
  - 直接从文件系统读取
  - 自动剥离 YAML frontmatter (`---`)
  - 通过 skill.json 过滤 enabled + channels
  - 完全向后兼容现有目录结构
```

### 6.2 数据库实现（`DbWorkspaceStorage`）

```
表结构:
  prompts:
    workspace_id | name (VARCHAR) | content (TEXT) |
    raw_content (TEXT) | frontmatter (JSON) |
    created_at | updated_at

  skills:
    workspace_id | name (VARCHAR) | content (TEXT) |
    enabled (BOOLEAN) | channels (JSON) |
    config (JSON) | metadata (JSON) |
    created_at | updated_at

特点:
  - read_prompts_batch() 用一次 SQL IN 查询批量加载
  - list_skill_entries() 支持 SQL 级别 channel 过滤
  - 不依赖具体数据库（SQLite / PostgreSQL 均可）
```

### 6.3 AgentBuilder 集成

```python
class AgentBuilder:
    def build(self, ctx: Any) -> Any:
        scene = getattr(ctx, "scene", None)
        user_id = getattr(ctx, "user_id", None)

        if scene and user_id:
            return self._build_composite(ctx, scene, user_id)

        # === 原有逻辑（不变） ===
        workspace_dir = ...
        ensure_skills_initialized(workspace_dir)
        effective_skills = resolve_effective_skills(...)
        local_ws = QwenPawLocalWorkspace(workspace_dir)
        ...

    def _build_composite(self, ctx, scene, user_id):
        scene_cfg = self._get_workspace_config("scenes", scene)
        user_cfg  = self._get_workspace_config("users", user_id)

        scene_storage = create_workspace_storage(scene_cfg)
        user_storage  = create_workspace_storage(user_cfg)

        resolver = CompositeWorkspaceResolver(scene_storage, user_storage)

        # Prompt
        ws_section = resolver.build_workspace_section()
        env_ctx = self._build_env_context(ctx, agent_config)
        sys_prompt = "\n\n".join([ws_section, env_ctx])

        # Skills → Tools
        skills = resolver.resolve_skills(channel_name)
        toolkit = await self._build_toolkit_from_skills(skills, ...)

        # Agent
        model, _ = self.build_model(agent_config)
        return QwenPawAgent(
            name=..., model=model,
            system_prompt=sys_prompt, toolkit=toolkit, ...
        )
```

### 6.4 文件清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `src/qwenpaw/storage/__init__.py` | 新增 | 导出接口 + 工厂 + 注册 |
| `src/qwenpaw/storage/interface.py` | 新增 | WorkspaceStorage ABC + 数据类 |
| `src/qwenpaw/storage/fs_storage.py` | 新增 | 文件系统实现 |
| `src/qwenpaw/storage/db_storage.py` | 新增 | 数据库实现 |
| `src/qwenpaw/runtime/composite_workspace.py` | 新增 | CompositeWorkspaceResolver |
| `src/qwenpaw/runtime/builder.py` | 修改 | 新增 `_build_composite()` 分支 |

---

## 7. 配置与部署

### 7.1 目录结构（文件系统模式）

```
~/.qwenpaw/
├── scenes/                          # 场景 workspace
│   ├── smart_query/
│   │   ├── SOUL.md
│   │   ├── AGENTS.md
│   │   ├── SCENE_CONFIG.md
│   │   ├── skill.json
│   │   └── skills/
│   │       ├── sql_generator/
│   │       └── chart_renderer/
│   │
│   ├── smart_doc/
│   │   ├── SOUL.md
│   │   ├── AGENTS.md
│   │   └── skills/
│   │       └── doc_writer/
│   │
│   └── kb_retrieval/
│       ├── SOUL.md
│       └── skills/
│           └── kb_search/
│
├── users/                           # 用户 workspace
│   ├── a/
│   │   ├── PROFILE.md
│   │   ├── MEMORY.md
│   │   └── skills/
│   │       └── custom_search/
│   │
│   ├── b/
│   └── ...
│
└── workspaces/                      # 向后兼容：原有单 workspace
    └── default/
```

### 7.2 Workspace 配置（`config/workspaces.yaml`）

```yaml
scenes:
  smart_query:
    id: scene_smart_query
    storage:
      type: filesystem
      dir: ~/.qwenpaw/scenes/smart_query

  smart_doc:
    id: scene_smart_doc
    storage:
      type: filesystem
      dir: ~/.qwenpaw/scenes/smart_doc

  kb_retrieval:
    id: scene_kb_retrieval
    storage:
      type: filesystem
      dir: ~/.qwenpaw/scenes/kb_retrieval

users:
  a:
    id: user_a
    storage:
      type: database
      dsn: sqlite:///home/qwenpaw/qwenpaw.db
      workspace_id: user_a

  b:
    id: user_b
    storage:
      type: filesystem
      dir: ~/.qwenpaw/users/b

  c:
    id: user_c
    storage:
      type: database
      dsn: postgresql://user:pass@host/qwenpaw
      workspace_id: user_c
```

### 7.3 请求格式

```json
POST /api/chat
{
    "scene": "smart_query",
    "user_id": "a",
    "channel": "dingtalk",
    "message": "上个月华南区销售额是多少？按月份画折线图"
}
```

### 7.4 向后兼容

不传 `scene` + `user_id` 时，完全走原有单 workspace 逻辑，不影响现有功能：

```json
POST /api/chat
{
    "message": "帮我写个 Python 脚本"
}
```

---

## 8. 附录

### 8.1 方案对比矩阵

| 维度 | 磁盘合并 | 内存合并（Prompt） | 最终方案（存储抽象+内存） |
|------|---------|-------------------|---------------------------|
| 覆盖 Prompt | ✅ | ✅ | ✅ |
| 覆盖 Skills | ✅ | ❌ | ✅ |
| 无额外 IO | ❌ | ✅ | ✅ |
| 无磁盘写入 | ❌ | ✅ | ✅ |
| 存储无关 | ❌ | ❌ | ✅ |
| 场景用FS+用户用DB | ❌ | ❌ | ✅ |
| 向后兼容 | ✅ | ✅ | ✅ |
| 内核改动量 | 极小 | 极极小 | 很小（只改builder.py） |

### 8.2 关键设计决策

| 决策 | 理由 |
|------|------|
| Skills 同名时用户覆盖场景 | 用户个性化应优先；场景技能作为默认能力 |
| AGENTS.md 场景 base + 用户 overlay | 场景定义 SOP，用户只需追加个性化偏好 |
| 不在 PromptManager 层做拦截 | PromptManager 设计为 Contributor 模式，拦截它会导致与 plugin 机制冲突 |
| 不做缓存（MVP） | 先保证正确性，后续可用场景级缓存 + 指纹增量更新 |
| 存储后端由配置驱动 | 允许按 workspace 粒度独立配置，无需改代码 |

### 8.3 后续优化方向

1. **场景级缓存**：场景 workspace 的 prompt + skills 通常不变，可缓存 `ParsedWorkspace` 对象
2. **增量更新**：通过 md5 指纹判断场景/用户文件是否变化，变化时才重新读取
3. **预热机制**：应用启动时预加载高频场景的存储后端实例
4. **管理界面**：Web 控制台中增加场景 workspace 和用户 workspace 的增删改查 UI
5. **Workspace 模板**：支持从模板创建场景 workspace（如内置"智能问数"模板）

---

> **作者**: ima.copilot  
> **项目**: QwenPaw Composite Workspace  
> **源码**: agentscope-ai/QwenPaw
