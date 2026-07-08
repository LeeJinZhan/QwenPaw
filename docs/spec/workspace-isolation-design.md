# 用户工作区强制隔离 设计方案（Landlock 版）

## 一、问题

现有设计（`tool-gateway-design.md` 第四节）提出按 `user_id` 分目录存放数据：

```
users/alice/workspaces/default/
users/bob/workspaces/default/
```

**但这只是目录结构上的"逻辑隔离"**。同一进程/同一 OS 用户下，alice 的 agent 通过 `read_file` / `write_file` / `execute_shell_command` 等工具依然可以访问 `users/bob/` 目录——因为没有任何机制阻止跨用户路径访问。

## 二、目标

**在工具执行层面强制隔离**：alice 的 agent 进程无法读写 bob 的工作区目录，反之亦然。无论通过哪种工具（文件工具、shell 命令、脚本执行），跨用户访问都必须被阻断。

## 三、方案总览

```
┌────────────────────────────────────────────────────────┐
│ Layer 1: Landlock 内核级沙箱（主力）                      │
│  allow_read_all=False → 纯白名单                         │
│  只挂载当前用户 workspace + 系统路径                       │
│  其他用户目录不在白名单中 → 内核拒绝 EACCES                │
├────────────────────────────────────────────────────────┤
│ Layer 2: FilePathToolGuardian 工作区边界守护（纵深）      │
│  所有文件工具 + shell 命令的路径参数先验归属               │
│  非当前用户 workspace → CRITICAL 阻断                     │
└────────────────────────────────────────────────────────┘
```

**Layer 1 是主力**（内核强制，覆盖一切子进程），**Layer 2 是纵深防御**（非 Linux 平台降级、或沙箱未启用的兜底）。

## 四、为什么选择 Landlock

| | Landlock | Bubblewrap |
|------|---------|---------|
| 安装依赖 | **无**，内核内置（5.13+） | 需安装 `bubblewrap` 包 |
| 权限模型 | **原生白名单**（没授权 = 自动拒绝） | bind mount 模拟（需手动构造文件系统视图） |
| 用户命名空间 | 不需要 | 必需（部分环境被禁用） |
| 规则语义 | path_beneath，子目录自动继承 | 需逐个路径挂载 |
| 部署复杂度 | 零 | 需确认 bwrap 可用性 + user namespace 开启 |

Landlock 的 `allow_read_all=False` 模式天然就是用户隔离所需要的模型——只授权当前用户的 workspace + 必要的系统路径，其余全部不可见。

## 五、Layer 1：Landlock 纯白名单隔离

### 5.1 核心原理

Landlock 是 deny-default 的安全模块。每个进程可以创建一个 ruleset，通过 `path_beneath` 规则声明"允许访问哪些目录"。未声明的路径自动被内核拒绝。

```
alice 的 agent 子进程:
  landlock_add_rule(path="/users/alice/workspaces/default/", access=RW)
  landlock_add_rule(path="/usr", access=RO)
  landlock_add_rule(path="/lib", access=RO)
  ...
  landlock_restrict_self()
  
  → 访问 /users/bob/  → 内核返回 EACCES
  → 访问 /etc/passwd  → 允许（/etc 在白名单中）
```

### 5.2 以 `user_id` 为参数的 SandboxConfig 生成

改动点只有一处：`ResourceGovernor.compile_sandbox_config()`。

```python
# governance/resource_governor.py

def compile_sandbox_config(
    self,
    tc_spec: ToolCallSpec,
    *,
    user_id: str | None = None,          # ★ 新增
) -> SandboxConfig:
    # 根据 user_id 解析用户专属工作区
    if user_id and self._multi_tenant_enabled:
        ws = str(self._resolve_user_workspace_dir(user_id))
    else:
        ws = str(self.workspace_dir)      # 向后兼容

    return SandboxConfig(
        mode=SandboxMode.LANDLOCK,
        workspace_dir=ws,
        mounts=[
            MountSpec(path=ws, writable=True, executable=True),
        ],
        allow_read_all=False,              # ★ 关键：纯白名单
        deny_paths=[],                     # 不需要了
        network_allow=["*"],
        timeout_seconds=60,
        env_vars={k: "" for k in self.policy.env_blacklist},
    )
```

**为什么 `allow_read_all=False` 比 `deny_paths` 更安全**：

```
❌ allow_read_all=True + deny_paths=["/users/"]
   → 需要依赖 linux_sandbox.py 的枚举逻辑
   → 枚举遗漏 = 隔离失效
   → 路径多了容易出错

✅ allow_read_all=False
   → 只显式授权白名单路径
   → 白名单外的路径内核自动拒绝
   → 不依赖枚举，零遗漏
```

### 5.3 `allow_read_all=False` 时的系统路径

`linux_sandbox.py` 的 `_generate_sandbox_script()` 在 `allow_read_all=False` 时已经硬编码了系统白名单（第 302-317 行），无需额外改动：

```python
system_read_paths = [
    "/usr", "/lib", "/lib64", "/etc",
    "/proc", "/sys", "/dev", "/run",
    "/bin", "/sbin",
]
# /tmp 单独授予读写权限
```

如果 agent 运行时需要访问 `/opt` 或其他系统路径，按需追加即可。

### 5.4 user_id 如何传递到 compile_sandbox_config

```
ChannelManager._consume_with_tracker(request)
  │  request.user_id = "alice"
  │
  ├─ UserWorkspaceResolver.get_workspace("default", "alice")
  │     → ~/.qwenpaw/users/alice/workspaces/default/
  │
  └─ AgentBuilder.build_toolkit()
       │  request_context = {"user_id": "alice", ...}
       │
       └─ PolicyGuardedTool.__call__()
            │  tc_spec.user_id = "alice"
            │
            └─ ResourceGovernor.compile_sandbox_config(tc_spec, user_id="alice")
                 → SandboxConfig(
                     mode=LANDLOCK,
                     workspace_dir=".../users/alice/workspaces/default/",
                     allow_read_all=False,
                   )
```

### 5.5 非 Landlock 环境

若平台不支持 Landlock（macOS、Windows、旧内核），`detect_platform_mode()` 返回 `NONE`，沙箱不生效。此时完全依靠 Layer 2 的 FilePathToolGuardian 进行应用级隔离。

可选方案：接入 E2B 云沙箱作为非 Linux 平台的远程隔离后端（一个沙箱 per user）。

## 六、Layer 2：FilePathToolGuardian 工作区边界守护

### 6.1 改造点

`FilePathToolGuardian`（`file_guardian.py`）当前只检查敏感文件列表。新增 `user_workspace_root` 参数，在 `_check_value` 中同时做跨用户边界检查。

```python
# security/tool_guard/guardians/file_guardian.py

class FilePathToolGuardian(BaseToolGuardian):
    def __init__(
        self,
        *,
        sensitive_files: Iterable[str] | None = None,
        user_workspace_root: str | None = None,   # ★ 新增
    ) -> None:
        ...
        self._user_workspace_root = (
            Path(user_workspace_root).resolve()
            if user_workspace_root else None
        )

    def _is_cross_user_access(self, abs_path: str) -> bool:
        if self._user_workspace_root is None:
            return False
        try:
            resolved = Path(abs_path).resolve(strict=False)
            root = str(self._user_workspace_root)
            return not str(resolved).startswith(root + os.sep) and str(resolved) != root
        except Exception:
            return True  # fail-closed

    def _check_value(self, tool_name, param_name, raw_value, findings, *, snippet=None):
        normalized_input = _sanitize_path_candidate(raw_value)
        abs_path = _normalize_path(normalized_input)

        # ★ 跨用户边界检查（优先级最高）
        if self._is_cross_user_access(abs_path):
            findings.append(GuardFinding(
                rule_id="CROSS_USER_ACCESS_BLOCKED",
                severity=GuardSeverity.CRITICAL,
                title="[CRITICAL] Cross-user workspace access blocked",
                ...
            ))
            return  # 已是最高级别，无需继续

        # 原有：敏感文件检查
        if self._is_sensitive(abs_path):
            findings.append(self._make_finding(...))
```

### 6.2 覆盖范围

`_TOOL_FILE_PARAMS` 已覆盖所有文件工具参数（`file_path`），加上 `_extract_paths_from_shell_command` 对 shell 命令的路径提取，覆盖所有入口：

| 工具 | 检查方式 |
|------|---------|
| `read_file` / `write_file` / `edit_file` / `append_file` | 直接取 `file_path` 参数 |
| `execute_shell_command` | 解析命令字符串提取路径候选（支持重定向、引号） |
| 其他所有工具 | 扫描所有字符串参数，过滤路径形态的 |

## 七、完整安全层次

```
┌──────────────────────────────────────────────┐
│ 1. 工作区路由       user_id → 独立目录          │  数据组织
├──────────────────────────────────────────────┤
│ 2. Tool Gateway     MCP 按 user 授权工具       │  准入控制
├──────────────────────────────────────────────┤
│ 3. Landlock 内核    allow_read_all=False       │  ★ 主力
│    纯白名单，未授权路径内核直接拒绝              │
├──────────────────────────────────────────────┤
│ 4. FilePathGuardian 工作区边界校验              │  ★ 纵深
│    所有路径参数先验归属                         │
├──────────────────────────────────────────────┤
│ 5. PolicyGuardedTool  运行时策略               │
├──────────────────────────────────────────────┤
│ 6. ToolGuardEngine    参数安全扫描             │
└──────────────────────────────────────────────┘
```

## 八、改动清单

| # | 文件 | 改动 | 复杂度 |
|---|------|------|--------|
| 1 | `governance/resource_governor.py` | `compile_sandbox_config` 增加 `user_id` 参数，`allow_read_all=False`，根据 user_id 解析 workspace 目录 | 小 |
| 2 | `security/tool_guard/guardians/file_guardian.py` | 新增 `user_workspace_root` 参数 + `_is_cross_user_access()` + `_check_value` 中增加边界检查 | 中 |
| 3 | `app/workspace/user_resolver.py` | **新增** `UserWorkspaceResolver`，按 `(agent_id, user_id)` 路由 workspace 目录 | 中 |
| 4 | `app/channels/manager.py` | `_consume_with_tracker` 中按 user_id 调用 UserWorkspaceResolver | 小 |
| 5 | `runtime/builder.py` | `build_toolkit` 时传递 user_id 到 guardian 和 sandbox config | 小 |

**不改的文件**：`linux_sandbox.py`（`allow_read_all=False` 的代码路径已就绪）、`config.py`（枚举值和 `SandboxConfig` 无需变化）。

## 九、安全验证场景

| 场景 | 阻断层 | 预期结果 |
|------|--------|---------|
| `read_file /users/bob/...chats.json` | Layer 2 | CRITICAL: cross-user access blocked |
| `cat /users/bob/...chats.json` | Layer 2（路径提取） | CRITICAL: cross-user access blocked |
| `find /users/ -name "*.json"` | Layer 1 | Landlock: Permission denied |
| `python -c "open('/users/bob/x').read()"` | Layer 1 | Landlock: Permission denied |
| macOS 上 `read_file /users/bob/...` | Layer 2 | CRITICAL: cross-user access blocked |
| `multi_tenant.enabled=false` | 无 | 所有新增检查跳过，行为不变 |

## 十、关键设计决策

| 决策 | 选择 | 理由 |
|------|------|------|
| 主力隔离机制 | Landlock `allow_read_all=False` | 纯白名单，内核原生语义，不依赖 denylist 枚举 |
| Sandbox 后端 | 仅 Landlock | 无安装依赖，部署成本为零 |
| 非 Linux 降级 | FilePathToolGuardian（可选：E2B 云沙箱） | 两级应用层守卫覆盖非 Linux 平台 |
| deny 策略 | **不需要 deny_paths** | 未授权的路径内核自动拒绝 |
| 新用户加入 | 无配置变更 | 白名单基于 user_id 动态生成，不感知其他用户 |
| 路径检查 | 所有字符串参数 | 防止未知工具绕过已知参数名列表 |
| 失败策略 | Fail-closed | 路径解析失败 → 拦截 |
| 向后兼容 | `multi_tenant.enabled` 开关 | 关闭后完全回到原有单 Workspace 模式 |
