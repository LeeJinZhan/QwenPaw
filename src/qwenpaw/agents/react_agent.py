# -*- coding: utf-8 -*-
"""QwenPaw Agent - Main agent implementation.

This module provides the main QwenPawAgent class built on ReActAgent,
with integrated tools, skills, and memory management.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterator, List, Literal, Optional, Type, TYPE_CHECKING

from agentscope.agent import ReActAgent
from agentscope.agent._react_agent import _MemoryMark
from agentscope.memory import InMemoryMemory
from agentscope.message import Msg
from agentscope.tool import Toolkit
from anyio import ClosedResourceError
from pydantic import BaseModel

from ..app.mcp import HttpStatefulClient, StdIOStatefulClient
from .command_handler import CommandHandler
from .hooks import BootstrapHook
from .model_factory import create_model_and_formatter
from .prompt import (
    build_multimodal_hint,
    build_system_prompt_from_working_dir,
    get_active_model_supports_multimodal,
)
from .skill_system import (
    apply_skill_config_env_overrides,
    ensure_skills_initialized,
    get_workspace_skills_dir,
    resolve_effective_skills,
)
from .coding_mode_mixin import CodingModeMixin
from .tool_guard_mixin import ToolGuardMixin
from .tools import (
    browser_use,
    delegate_external_agent,
    chat_with_agent,
    check_agent_task,
    spawn_subagent,
    submit_to_agent,
    desktop_screenshot,
    edit_file,
    execute_shell_command,
    get_current_time,
    get_token_usage,
    glob_search,
    grep_search,
    list_agents,
    materialize_skill,
    read_file,
    runtime_attachment_read,
    runtime_sandbox_files_search,
    runtime_tool_gateway,
    run_tool_batch,
    send_file_to_user,
    set_user_timezone,
    view_image,
    view_video,
    write_file,
)
from .tools import runtime_sandbox_oss
from .utils import process_file_and_media_blocks_in_message
from ..constant import (
    MEDIA_UNSUPPORTED_PLACEHOLDER,
    WORKING_DIR,
)
from ..providers.model_capability_cache import get_capability_cache

if TYPE_CHECKING:
    from ..agents.memory import BaseMemoryManager
    from ..agents.context import BaseContextManager
    from ..config.config import AgentProfileConfig
    from .context import AgentContext

logger = logging.getLogger(__name__)

# Valid namesake strategies for tool registration
NamesakeStrategy = Literal["override", "skip", "raise", "rename"]


def _build_runtime_tool_gateway_context(request_context: dict[str, Any]) -> str:
    """Render Runtime Tool Gateway instructions for bank-runtime calls."""
    gateway = request_context.get("runtime_tool_gateway")
    if not isinstance(gateway, dict):
        return ""
    allowed_tools = [
        str(item).strip()
        for item in gateway.get("allowed_tools", [])
        if str(item).strip()
    ]
    constraints = request_context.get("runtime_constraints")
    disabled_tools: list[str] = []
    if isinstance(constraints, dict):
        disabled_tools = [
            str(item).strip()
            for item in constraints.get("disabled_tools", [])
            if str(item).strip()
        ]
    task_id = str(gateway.get("task_id", "")).strip()
    lines = [
        "Runtime Tool Gateway is enabled for this request.",
        "- Runtime-governed requests must not use native QwenPaw tools.",
        "- Do not use native shell, file, browser, desktop, or other "
        "QwenPaw tools to simulate Runtime actions.",
    ]
    if allowed_tools:
        lines.append(
            "- Use the QwenPaw tool `runtime_tool_gateway` for Runtime "
            "actions.",
        )
        lines.append("- Allowed Runtime tool ids: " + ", ".join(allowed_tools))
    else:
        lines.append(
            "- No Runtime Tool Gateway tool ids are currently allowed; "
            "answer without gateway tools or explain that the requested "
            "capability is unavailable.",
        )
    if "workspace.list_outputs" in allowed_tools and task_id:
        lines.append(
            "- For `workspace.list_outputs`, call "
            "`runtime_tool_gateway` with "
            f"`tool_id=\"workspace.list_outputs\"` and "
            f"`input={{\"task_id\":\"{task_id}\"}}`.",
        )
    if disabled_tools:
        lines.append("- Disabled native tools: " + ", ".join(disabled_tools))
    attachment_lines = _build_runtime_attachments_context(request_context)
    if attachment_lines:
        lines.extend(attachment_lines)
    lines.append(
        "- Native QwenPaw memory tools, including `memory_search`, are "
        "disabled in Runtime-governed requests.",
    )
    return "\n".join(lines)


def _build_runtime_attachments_context(
    request_context: dict[str, Any],
) -> list[str]:
    """Render safe Runtime attachment hints without grant URLs or tokens."""
    manifest = _runtime_attachments_manifest(request_context)
    sandbox_context = request_context.get("sandbox_context")
    if not isinstance(sandbox_context, dict):
        return []
    lines = [
        "- 优先使用本次任务已附带的文件。只在当前文件不足以完成请求时，",
        "  才查找当前会话或当前用户+助手工作区的补充文件。",
        "- 只能读取搜索结果返回的 file_id，不得构造路径或对象键。",
        "- Use `runtime_sandbox_files_search` only for supplemental files, "
        "then use `runtime_attachment_read(file_id=...)` with a returned "
        "file_id.",
        "- Do not request, reveal, copy, or summarize Runtime attachment "
        "URLs, headers, tokens, object keys, storage locations, credentials, or "
        "local paths.",
    ]
    safe_items: list[str] = []
    for item in manifest:
        file_id = str(item.get("file_id", "")).strip()
        if not file_id:
            continue
        original_name = str(item.get("original_name", "")).strip()
        content_type = str(item.get("content_type", "")).strip()
        size_bytes = str(item.get("size_bytes", "")).strip()
        expires_at = str(item.get("expires_at", "")).strip()
        summary = f"  - file_id={file_id}"
        if original_name:
            summary += f", name={original_name}"
        if content_type:
            summary += f", content_type={content_type}"
        if size_bytes:
            summary += f", size_bytes={size_bytes}"
        if expires_at:
            summary += f", expires_at={expires_at}"
        safe_items.append(summary)
    if safe_items:
        lines.append("- Authorized Runtime attachments:")
        lines.extend(safe_items)
    return lines


def _runtime_attachments_manifest(
    request_context: dict[str, Any],
) -> list[dict[str, Any]]:
    manifest = request_context.get("attachments_manifest")
    if not isinstance(manifest, list):
        return []
    return [item for item in manifest if isinstance(item, dict)]


def _runtime_attachments_available(request_context: dict[str, Any]) -> bool:
    return isinstance(
        request_context.get("sandbox_context"),
        dict,
    )


def _runtime_current_task_attachment_ids(request_context: dict[str, Any]) -> list[str]:
    file_ids: list[str] = []
    for item in _runtime_attachments_manifest(request_context):
        if str(item.get("source", "")).strip() != "current_task":
            continue
        file_id = str(item.get("file_id", "")).strip()
        if file_id:
            file_ids.append(file_id)
    return file_ids


async def _append_runtime_attachment_content_parts(
    msg: Msg | list[Msg] | None,
    request_context: dict[str, Any],
) -> Msg | list[Msg] | None:
    if msg is None:
        return msg
    file_ids = _runtime_current_task_attachment_ids(request_context)
    if not file_ids:
        return msg
    sandbox_context = request_context.get("sandbox_context")
    if not isinstance(sandbox_context, dict):
        raise runtime_sandbox_oss.RuntimeAttachmentPreparationError(
            file_ids[0],
            "SANDBOX_CONTEXT_INVALID",
        )
    task_id = str(sandbox_context.get("task_id") or "").strip()
    if not task_id:
        raise runtime_sandbox_oss.RuntimeAttachmentPreparationError(
            file_ids[0],
            "SANDBOX_CONTEXT_INVALID",
        )
    target = msg[-1] if isinstance(msg, list) and msg else msg
    if not isinstance(target, Msg):
        return msg
    try:
        cache = runtime_sandbox_oss._DEFAULT_TASK_ATTACHMENT_CACHE
        prepared_files = await runtime_sandbox_oss.run_task_io_in_thread(
            cache,
            task_id,
            cache.prepare_files,
            file_ids,
            sandbox_context,
            file_id=file_ids[0],
        )
    except runtime_sandbox_oss.RuntimeAttachmentPreparationError:
        raise
    except Exception as exc:
        raise runtime_sandbox_oss.RuntimeAttachmentPreparationError(
            file_ids[0],
            "ATTACHMENT_READ_FAILED",
        ) from exc
    if isinstance(target.content, list):
        content_parts = target.content
    else:
        content_parts = [{"type": "text", "text": target.get_text_content()}]
        target.content = content_parts
    for prepared in prepared_files:
        content_parts.append(
            runtime_sandbox_oss.content_part_for_prepared_file(prepared),
        )
    return msg


def _runtime_disabled_tools_from_context(
    request_context: dict[str, Any],
) -> set[str]:
    """Return native tools disabled by Runtime constraints."""
    constraints = request_context.get("runtime_constraints")
    if not isinstance(constraints, dict):
        return set()
    return {
        str(item).strip()
        for item in constraints.get("disabled_tools", [])
        if str(item).strip()
    }


def _runtime_simple_text_fast_enabled_from_context(
    request_context: dict[str, Any],
) -> bool:
    return str(request_context.get("runtime_execution_mode", "")).strip() == (
        "simple_text_fast"
    )


def _runtime_disable_thinking_from_context(
    request_context: dict[str, Any],
) -> bool:
    controls = request_context.get("runtime_generation_controls")
    if not isinstance(controls, dict):
        return False
    if controls.get("disable_thinking") is True:
        return True
    thinking = str(controls.get("thinking", "") or "").strip().lower()
    return thinking in {"disabled", "disable", "off", "false", "0"}


def _iter_model_generate_kwargs_targets(model: Any) -> Iterator[Any]:
    seen: set[int] = set()
    stack: list[Any] = [model]
    while stack:
        current = stack.pop()
        if current is None:
            continue
        current_id = id(current)
        if current_id in seen:
            continue
        seen.add(current_id)
        if isinstance(getattr(current, "generate_kwargs", None), dict):
            yield current
        for attr in ("_inner", "_model", "inner", "model"):
            child = getattr(current, attr, None)
            if child is not None and id(child) not in seen:
                stack.append(child)


@contextmanager
def _temporarily_disable_model_thinking(model: Any) -> Iterator[None]:
    snapshots: list[tuple[Any, dict[str, Any]]] = []
    for target in _iter_model_generate_kwargs_targets(model):
        original = getattr(target, "generate_kwargs", None)
        if not isinstance(original, dict):
            continue
        updated = deepcopy(original)
        extra_body = updated.get("extra_body")
        if not isinstance(extra_body, dict):
            extra_body = {}
        else:
            extra_body = deepcopy(extra_body)
        extra_body["enable_thinking"] = False
        updated["extra_body"] = extra_body
        snapshots.append((target, deepcopy(original)))
        target.generate_kwargs = updated
    try:
        yield
    finally:
        for target, original in reversed(snapshots):
            target.generate_kwargs = original


def _build_runtime_simple_text_fast_prompt(
    request_context: dict[str, Any],
    *,
    language: str,
) -> str:
    task_id = str(request_context.get("runtime_task_id", "") or "").strip()
    trace_id = str(request_context.get("trace_id", "") or "").strip()
    lines = [
        "你是 Bank Agent Runtime 通过 QwenPaw 调用的轻量文本助手。",
        "直接回答用户的简单文本问题，保持简洁、准确，并使用用户的语言。",
        "不要调用工具，不要读写文件，不要访问浏览器、shell、外部网络、银行数据库或未授权 MCP。",
        "如果用户问题需要附件、客户数据、外部系统、工具执行或高风险能力，说明该请求需要走完整任务模式。",
        "不要声称已经创建文件、执行命令、查询客户信息或调用外部系统。",
    ]
    if task_id:
        lines.append(f"Runtime task_id: {task_id}")
    if trace_id:
        lines.append(f"Runtime trace_id: {trace_id}")
    datetime_context = request_context.get("runtime_datetime_context")
    if isinstance(datetime_context, dict):
        current_date = str(datetime_context.get("current_date", "") or "").strip()
        current_datetime = str(
            datetime_context.get("current_datetime", "") or "",
        ).strip()
        timezone = str(datetime_context.get("timezone", "") or "").strip()
        if current_date or current_datetime or timezone:
            lines.append("Runtime time context:")
            if current_date:
                lines.append(f"- Current date: {current_date}")
            if current_datetime:
                lines.append(f"- Current datetime: {current_datetime}")
            if timezone:
                lines.append(f"- Timezone: {timezone}")
            lines.append(
                "当用户询问今天、当前日期或当前时间时，必须基于上述 Runtime time context 回答。",
            )
    if language:
        lines.append(f"Preferred language: {language}")
    return "\n".join(lines)


class QwenPawAgent(CodingModeMixin, ToolGuardMixin, ReActAgent):
    """QwenPaw Agent with integrated tools, skills, and memory management.

    This agent extends ReActAgent with:
    - Built-in tools (shell, file operations, browser, etc.)
    - Dynamic skill loading from working directory
    - Memory management with auto-compaction
    - Bootstrap guidance for first-time setup
    - System command handling (/compact, /new, etc.)
    - Tool-guard security interception (via ToolGuardMixin)
    - Coding Mode features: Inline Diff (via CodingModeMixin)

    MRO note
    ~~~~~~~~
    MRO: QwenPawAgent → CodingModeMixin → ToolGuardMixin → ReActAgent.
    Each ``_acting`` override **must** call ``super()._acting(...)`` so
    the full chain stays active.
    """

    def __init__(
        self,
        agent_config: "AgentProfileConfig",
        env_context: Optional[str] = None,
        mcp_clients: Optional[List[Any]] = None,
        memory_manager: BaseMemoryManager | None = None,
        context_manager: BaseContextManager | None = None,
        request_context: Optional[dict[str, str]] = None,
        namesake_strategy: NamesakeStrategy = "skip",
        workspace_dir: Path | None = None,
        task_tracker: Any | None = None,
        plan_notebook: Any | None = None,
    ):
        """Initialize QwenPawAgent.

        Args:
            agent_config: Agent profile configuration containing all settings
                including running config (max_iters, max_input_length,
                memory_compact_threshold, etc.) and language setting.
            env_context: Optional environment context to prepend to
                system prompt
            mcp_clients: Optional list of MCP clients for tool
                integration
            memory_manager: Optional memory manager instance. Pass ``None``
                to disable the memory manager entirely.
            context_manager: Optional context manager instance
            request_context: Optional request context with session_id,
                user_id, channel, agent_id
            namesake_strategy: Strategy to handle namesake tool functions.
                Options: "override", "skip", "raise", "rename"
                (default: "skip")
            workspace_dir: Workspace directory for reading prompt files
                (if None, uses global WORKING_DIR)
        """
        self._agent_config = agent_config
        self._env_context = env_context
        self._request_context = dict(request_context or {})
        self._mcp_clients = mcp_clients or []
        self._namesake_strategy = namesake_strategy
        self._workspace_dir = workspace_dir
        self._task_tracker = task_tracker
        simple_text_fast = self._runtime_simple_text_fast_enabled()

        # Extract configuration from agent_config
        running_config = agent_config.running
        self._language = agent_config.language

        # Resolve effective skills once and share across toolkit /
        # skill registration.
        if simple_text_fast:
            effective_skills = []
        else:
            workspace_dir = self._workspace_dir or WORKING_DIR
            ensure_skills_initialized(workspace_dir)
            channel_name = self._request_context.get("channel", "console")
            try:
                effective_skills = resolve_effective_skills(
                    workspace_dir,
                    channel_name,
                )
            except Exception:  # pylint: disable=broad-except
                effective_skills = []

        # Initialize toolkit with built-in tools
        toolkit = self._create_toolkit(
            namesake_strategy=namesake_strategy,
            effective_skills=effective_skills,
        )

        # Load and register skills
        self._register_skills(toolkit, effective_skills=effective_skills)

        # Initialize memory_manager and context_manager for use
        # in _build_sys_prompt
        self.memory_manager = None if simple_text_fast else memory_manager
        self.context_manager = None if simple_text_fast else context_manager

        # Build system prompt
        sys_prompt = self._build_sys_prompt()

        # Create model and formatter using factory method
        model, formatter = create_model_and_formatter(agent_id=agent_config.id)
        model_info = (
            f"{agent_config.active_model.provider_id}/"
            f"{agent_config.active_model.model}"
            if agent_config.active_model
            else "global-fallback"
        )
        logger.info(
            f"Agent '{agent_config.id}' initialized with model: "
            f"{model_info} (class: {model.__class__.__name__})",
        )
        # Initialize parent ReActAgent
        init_kwargs: dict[str, Any] = {
            "name": agent_config.name or "QwenPaw",
            "model": model,
            "sys_prompt": sys_prompt,
            "toolkit": toolkit,
            "memory": InMemoryMemory(),
            "formatter": formatter,
            "max_iters": running_config.max_iters,
        }
        if plan_notebook is not None:
            init_kwargs["plan_notebook"] = plan_notebook
        super().__init__(**init_kwargs)

        # Register memory tools provided by the memory manager
        self._register_memory_tools()

        # Configure context manager memory if available
        if self.context_manager is not None:
            self.memory: "AgentContext" = (
                self.context_manager.get_agent_context()
            )
            logger.debug("Context manager configured")

        # Setup command handler
        self.command_handler = CommandHandler(
            agent_name=self.name,
            memory=self.memory,
            memory_manager=self.memory_manager,
            context_manager=self.context_manager,
        )

        # Register hooks
        self._register_hooks()

    def _create_toolkit(  # pylint: disable=too-many-branches
        self,
        namesake_strategy: NamesakeStrategy = "skip",
        effective_skills: list[str] | None = None,
    ) -> Toolkit:
        """Create and populate toolkit with built-in tools.

        Args:
            namesake_strategy: Strategy to handle namesake tool functions.
                Options: "override", "skip", "raise", "rename"
                (default: "skip")
            effective_skills: Skills enabled for this workspace + channel,
                used to gate skill-specific tools.

        Returns:
            Configured toolkit instance
        """
        effective_skills = effective_skills or []
        if _runtime_simple_text_fast_enabled_from_context(
            getattr(self, "_request_context", {}) or {},
        ):
            return Toolkit()
        toolkit = Toolkit()

        # Check which tools are enabled from agent config
        enabled_tools = {}
        async_execution_tools = {}
        try:
            if hasattr(self._agent_config, "tools") and hasattr(
                self._agent_config.tools,
                "builtin_tools",
            ):
                builtin_tools = self._agent_config.tools.builtin_tools
                enabled_tools = {
                    name: tool.enabled for name, tool in builtin_tools.items()
                }
                # Only selected long-running tools support async_execution.
                async_capable_tool_names = {
                    "execute_shell_command",
                    "delegate_external_agent",
                }
                async_execution_tools = {
                    name: (
                        builtin_tools.get(name).async_execution
                        if name in builtin_tools
                        else False
                    )
                    for name in async_capable_tool_names
                }
        except Exception as e:
            logger.warning(
                f"Failed to load agent tools config: {e}, "
                "all tools will be disabled",
            )

        runtime_gateway_enabled = self._runtime_tool_gateway_enabled()
        runtime_allowed_native_tools = {"runtime_tool_gateway"}
        runtime_attachment_available = _runtime_attachments_available(
            self._request_context,
        )
        if runtime_attachment_available:
            runtime_allowed_native_tools.add("runtime_attachment_read")
            runtime_allowed_native_tools.add("runtime_sandbox_files_search")

        # Map of tool functions (hardcoded builtin tools)
        tool_functions = {
            "execute_shell_command": execute_shell_command,
            "read_file": read_file,
            "write_file": write_file,
            "edit_file": edit_file,
            "grep_search": grep_search,
            "glob_search": glob_search,
            "browser_use": browser_use,
            "desktop_screenshot": desktop_screenshot,
            "view_image": view_image,
            "view_video": view_video,
            "send_file_to_user": send_file_to_user,
            "get_current_time": get_current_time,
            **(
                {"runtime_tool_gateway": runtime_tool_gateway}
                if runtime_gateway_enabled
                else {}
            ),
            **(
                {"runtime_attachment_read": runtime_attachment_read}
                if runtime_gateway_enabled and runtime_attachment_available
                else {}
            ),
            **(
                {"runtime_sandbox_files_search": runtime_sandbox_files_search}
                if runtime_gateway_enabled and runtime_attachment_available
                else {}
            ),
            "set_user_timezone": set_user_timezone,
            "get_token_usage": get_token_usage,
            "delegate_external_agent": delegate_external_agent,
            "list_agents": list_agents,
            "chat_with_agent": chat_with_agent,
            "submit_to_agent": submit_to_agent,
            "check_agent_task": check_agent_task,
            "spawn_subagent": spawn_subagent,
            "run_tool_batch": run_tool_batch,
            # Register only when the `make-skill` skill is enabled.
            **(
                {"materialize_skill": materialize_skill}
                if "make-skill" in effective_skills
                else {}
            ),
        }

        # Track hardcoded built-in tools for backward compatibility
        hardcoded_builtin_tools = set(tool_functions.keys())

        # Dynamically load plugin-registered tools
        from . import tools as tools_module

        plugin_tools = set()
        for tool_name in getattr(tools_module, "__all__", []):
            if tool_name not in tool_functions:
                tool_func = getattr(tools_module, tool_name, None)
                if callable(tool_func):
                    tool_functions[tool_name] = tool_func
                    plugin_tools.add(tool_name)
                    logger.debug(
                        "Discovered plugin tool: %s",
                        tool_name,
                    )

        # Register tools with appropriate defaults
        runtime_disabled_tools = _runtime_disabled_tools_from_context(
            self._request_context,
        )
        registered_tool_names: set[str] = set()
        for tool_name, tool_func in tool_functions.items():
            if runtime_gateway_enabled and tool_name not in runtime_allowed_native_tools:
                logger.debug(
                    "Skipped native tool while Runtime Tool Gateway is "
                    "enabled: %s",
                    tool_name,
                )
                continue
            if tool_name in runtime_disabled_tools:
                logger.debug("Skipped Runtime-disabled tool: %s", tool_name)
                continue
            # For plugin tools: skip if not in config (security)
            # For hardcoded tools: default to enabled (backward compatibility)
            if tool_name in plugin_tools:
                if tool_name not in enabled_tools:
                    logger.debug(
                        "Skipped unconfigured plugin tool: %s",
                        tool_name,
                    )
                    continue
            else:
                # Hardcoded built-in tool: use default-to-enabled
                pass

            # Check if tool is enabled
            if not enabled_tools.get(
                tool_name,
                tool_name in hardcoded_builtin_tools,
            ):
                logger.debug("Skipped disabled tool: %s", tool_name)
                continue

            # Get async_execution setting (default to False for backward
            # compatibility)
            async_exec = async_execution_tools.get(tool_name, False)

            toolkit.register_tool_function(
                tool_func,
                namesake_strategy=namesake_strategy,
                async_execution=async_exec,
            )
            logger.debug(
                "Registered tool: %s (async_execution=%s)",
                tool_name,
                async_exec,
            )
            registered_tool_names.add(tool_name)

        # Auto-register background task management tools if any *enabled*
        # tool has async_execution set
        has_async_tools = any(
            async_execution_tools.get(name, False)
            for name in registered_tool_names
        )
        if has_async_tools:
            try:
                toolkit.register_tool_function(
                    toolkit.view_task,
                    namesake_strategy=namesake_strategy,
                )
                toolkit.register_tool_function(
                    toolkit.wait_task,
                    namesake_strategy=namesake_strategy,
                )
                toolkit.register_tool_function(
                    toolkit.cancel_task,
                    namesake_strategy=namesake_strategy,
                )
                logger.debug(
                    "Registered background task management tools "
                    "(view_task, wait_task, cancel_task)",
                )
            except Exception as e:
                logger.warning(
                    f"Failed to register task management tools: {e}",
                )

        # Coding Mode tools (lsp, ast_search) — only registered when
        # coding_mode.enabled is True and the underlying CLI / language
        # server is reachable.  See CodingModeMixin.
        try:
            self._register_coding_mode_tools(
                toolkit,
                namesake_strategy=namesake_strategy,
            )
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(f"Failed to register Coding Mode tools: {e}")

        return toolkit

    def _register_skills(
        self,
        toolkit: Toolkit,
        effective_skills: list[str],
    ) -> None:
        """Load and register skills from workspace directory.

        Args:
            toolkit: Toolkit to register skills to
            effective_skills: Resolved skill names for the current
                workspace + channel.
        """
        workspace_dir = self._workspace_dir or WORKING_DIR
        working_skills_dir = get_workspace_skills_dir(Path(workspace_dir))

        for skill_name in effective_skills:
            skill_dir = working_skills_dir / skill_name
            if skill_dir.exists():
                try:
                    toolkit.register_agent_skill(str(skill_dir))
                    logger.debug("Registered skill: %s", skill_name)
                except Exception as e:
                    logger.error(
                        "Failed to register skill '%s': %s",
                        skill_name,
                        e,
                    )

    def _register_memory_tools(self) -> None:
        """Register memory tools unless Runtime owns tool governance."""
        if self.memory_manager is None:
            return
        if _runtime_simple_text_fast_enabled_from_context(
            getattr(self, "_request_context", {}) or {},
        ):
            logger.debug("Skipped native memory tools in simple_text_fast mode")
            return
        if self._runtime_tool_gateway_enabled():
            logger.debug(
                "Skipped native memory tools while Runtime Tool Gateway "
                "is enabled",
            )
            return
        memory_tools = self.memory_manager.list_memory_tools()
        for tool_fn in memory_tools:
            self.toolkit.register_tool_function(
                tool_fn,
                namesake_strategy=self._namesake_strategy,
            )
        logger.debug(
            "Registered memory tools: %s",
            [fn.__name__ for fn in memory_tools],
        )

    def _build_sys_prompt(self) -> str:
        """Build system prompt from working dir files and env context.

        Returns:
            Complete system prompt string
        """
        if _runtime_simple_text_fast_enabled_from_context(
            getattr(self, "_request_context", {}) or {},
        ):
            return _build_runtime_simple_text_fast_prompt(
                self._request_context,
                language=self._language,
            )

        # Get agent_id from request_context
        agent_id = (
            self._request_context.get("agent_id")
            if self._request_context
            else None
        )

        # Check if heartbeat is enabled in agent config
        heartbeat_enabled = False
        if (
            hasattr(self._agent_config, "heartbeat")
            and self._agent_config.heartbeat is not None
        ):
            heartbeat_enabled = self._agent_config.heartbeat.enabled

        prompt_memory_manager = (
            None
            if self._runtime_tool_gateway_enabled()
            else self.memory_manager
        )
        sys_prompt = build_system_prompt_from_working_dir(
            working_dir=self._workspace_dir,
            agent_id=agent_id,
            heartbeat_enabled=heartbeat_enabled,
            language=self._language,
            memory_manager=prompt_memory_manager,
        )
        logger.debug("System prompt:\n%s...", sys_prompt[:100])

        from .prompt_builder import PromptBuilder
        from ..plugins.registry import PluginRegistry

        builder = PromptBuilder(PluginRegistry())
        sys_prompt = builder.build(
            agent=self,
            agent_id=agent_id,
            workspace=sys_prompt,
            multimodal=build_multimodal_hint() or "",
            env_context=self._env_context or "",
        )

        runtime_gateway_context = _build_runtime_tool_gateway_context(
            self._request_context,
        )
        if runtime_gateway_context:
            sys_prompt = sys_prompt + "\n\n" + runtime_gateway_context

        return sys_prompt

    def _runtime_tool_gateway_enabled(self) -> bool:
        gateway = self._request_context.get("runtime_tool_gateway")
        return isinstance(gateway, dict)

    def _runtime_simple_text_fast_enabled(self) -> bool:
        return _runtime_simple_text_fast_enabled_from_context(
            self._request_context,
        )

    def _register_hooks(self) -> None:
        """Register pre-reasoning and pre-acting hooks."""
        # Bootstrap hook - checks BOOTSTRAP.md on first interaction
        # Use workspace_dir if available, else fallback to WORKING_DIR
        working_dir = (
            self._workspace_dir if self._workspace_dir else WORKING_DIR
        )
        bootstrap_hook = BootstrapHook(
            working_dir=working_dir,
            language=self._language,
        )
        self.register_instance_hook(
            hook_type="pre_reasoning",
            hook_name="bootstrap_hook",
            hook=bootstrap_hook.__call__,
        )
        logger.debug("Registered bootstrap hook")

        # Context manager hooks - delegate compaction / tool-result pruning
        # to the context manager's lifecycle methods
        if self.context_manager is not None:
            self.register_instance_hook(
                hook_type="pre_reply",
                hook_name="context_pre_reply",
                hook=self.context_manager.pre_reply,
            )
            self.register_instance_hook(
                hook_type="pre_reasoning",
                hook_name="context_pre_reasoning",
                hook=self.context_manager.pre_reasoning,
            )
            self.register_instance_hook(
                hook_type="post_acting",
                hook_name="context_post_acting",
                hook=self.context_manager.post_acting,
            )
            self.register_instance_hook(
                hook_type="post_reply",
                hook_name="context_post_reply",
                hook=self.context_manager.post_reply,
            )
            logger.debug("Registered context manager hooks")

    def rebuild_sys_prompt(self) -> None:
        """Rebuild and replace the system prompt.

        Useful after load_session_state to ensure the prompt reflects
        the latest AGENTS.md / SOUL.md / PROFILE.md on disk.

        Updates both self._sys_prompt and the first system-role
        message stored in self.memory.content (if one exists).
        """
        self._sys_prompt = self._build_sys_prompt()

        if self.memory is None:
            logger.warning(
                "rebuild_sys_prompt: self.memory is None, "
                "skipping in-memory system prompt update.",
            )
            return

        for msg, _marks in self.memory.content:
            if msg.role == "system":
                msg.content = self.sys_prompt
            break

    async def register_mcp_clients(
        self,
        namesake_strategy: NamesakeStrategy = "skip",
    ) -> None:
        """Register MCP clients on this agent's toolkit after construction.

        Args:
            namesake_strategy: Strategy to handle namesake tool functions.
                Options: "override", "skip", "raise", "rename"
                (default: "skip")
        """
        for i, client in enumerate(self._mcp_clients):
            client_name = getattr(client, "name", repr(client))
            try:
                await self.toolkit.register_mcp_client(
                    client,
                    namesake_strategy=namesake_strategy,
                    execution_timeout=client.read_timeout_seconds,
                )
            except (ClosedResourceError, asyncio.CancelledError) as error:
                if self._should_propagate_cancelled_error(error):
                    raise
                logger.warning(
                    "MCP client '%s' session interrupted while listing tools; "
                    "trying recovery",
                    client_name,
                )
                recovered_client = await self._recover_mcp_client(client)
                if recovered_client is not None:
                    self._mcp_clients[i] = recovered_client
                    try:
                        await self.toolkit.register_mcp_client(
                            recovered_client,
                            namesake_strategy=namesake_strategy,
                            execution_timeout=client.read_timeout_seconds,
                        )
                        continue
                    except asyncio.CancelledError as recover_error:
                        if self._should_propagate_cancelled_error(
                            recover_error,
                        ):
                            raise
                        logger.warning(
                            "MCP client '%s' registration cancelled after "
                            "recovery, skipping",
                            client_name,
                        )
                    except Exception as e:  # pylint: disable=broad-except
                        logger.warning(
                            "MCP client '%s' still unavailable after "
                            "recovery, skipping: %s",
                            client_name,
                            e,
                        )
                else:
                    logger.warning(
                        "MCP client '%s' recovery failed, skipping",
                        client_name,
                    )
            except Exception as e:  # pylint: disable=broad-except
                logger.warning(
                    "Failed to register MCP client '%s', skipping: %s",
                    client_name,
                    e,
                    exc_info=True,
                )

    async def _recover_mcp_client(self, client: Any) -> Any | None:
        """Recover MCP client from broken session and return healthy client."""
        if await self._reconnect_mcp_client(client):
            return client

        rebuilt_client = self._rebuild_mcp_client(client)
        if rebuilt_client is None:
            return None

        if await self._reconnect_mcp_client(rebuilt_client):
            return self._reuse_shared_client_reference(
                original_client=client,
                rebuilt_client=rebuilt_client,
            )

        return None

    @staticmethod
    def _reuse_shared_client_reference(
        original_client: Any,
        rebuilt_client: Any,
    ) -> Any:
        """Keep manager-shared client reference stable after rebuild."""
        original_dict = getattr(original_client, "__dict__", None)
        rebuilt_dict = getattr(rebuilt_client, "__dict__", None)
        if isinstance(original_dict, dict) and isinstance(rebuilt_dict, dict):
            original_dict.update(rebuilt_dict)
            return original_client
        return rebuilt_client

    @staticmethod
    def _should_propagate_cancelled_error(error: BaseException) -> bool:
        """Only swallow MCP-internal cancellations, not task cancellation."""
        if not isinstance(error, asyncio.CancelledError):
            return False

        task = asyncio.current_task()
        if task is None:
            return False

        cancelling = getattr(task, "cancelling", None)
        if callable(cancelling):
            return cancelling() > 0

        # Python < 3.11: Task.cancelling() is unavailable.
        # Fall back to propagating CancelledError to avoid swallowing
        # genuine task cancellations when we cannot inspect the state.
        return True

    @staticmethod
    async def _reconnect_mcp_client(
        client: Any,
        timeout: float = 60.0,
    ) -> bool:
        """Best-effort reconnect for stateful MCP clients."""
        close_fn = getattr(client, "close", None)
        if callable(close_fn):
            try:
                await close_fn()
            except asyncio.CancelledError:  # pylint: disable=try-except-raise
                raise
            except Exception:  # pylint: disable=broad-except
                pass

        connect_fn = getattr(client, "connect", None)
        if not callable(connect_fn):
            return False

        try:
            await asyncio.wait_for(connect_fn(), timeout=timeout)
            return True
        except asyncio.CancelledError:  # pylint: disable=try-except-raise
            raise
        except asyncio.TimeoutError:
            return False
        except Exception:  # pylint: disable=broad-except
            return False

    @staticmethod
    def _rebuild_mcp_client(client: Any) -> Any | None:
        """Rebuild a fresh MCP client instance from stored config metadata."""
        rebuild_info = getattr(client, "_qwenpaw_rebuild_info", None)
        if not isinstance(rebuild_info, dict):
            return None

        transport = rebuild_info.get("transport")
        name = rebuild_info.get("name")

        try:
            if transport == "stdio":
                rebuilt_client = StdIOStatefulClient(
                    name=name,
                    command=rebuild_info.get("command"),
                    args=rebuild_info.get("args", []),
                    env=rebuild_info.get("env", {}),
                    cwd=rebuild_info.get("cwd"),
                )
                setattr(rebuilt_client, "_qwenpaw_rebuild_info", rebuild_info)
                return rebuilt_client

            raw_headers = rebuild_info.get("headers") or {}
            headers = (
                {k: os.path.expandvars(v) for k, v in raw_headers.items()}
                if raw_headers
                else None
            )
            rebuilt_client = HttpStatefulClient(
                name=name,
                transport=transport,
                url=rebuild_info.get("url"),
                headers=headers,
            )
            setattr(rebuilt_client, "_qwenpaw_rebuild_info", rebuild_info)
            return rebuilt_client
        except Exception:  # pylint: disable=broad-except
            return None

    # ------------------------------------------------------------------
    # Media-block fallback: strip unsupported media blocks (image, audio,
    # video, file) from memory and retry when the model rejects them.
    # Unlike model_factory._fixup_media_list (which converts file blocks
    # to text placeholders so the user-facing message history stays
    # readable), this fallback strips them entirely — its purpose is to
    # make a previously-rejected request retryable, so leaving residue
    # would defeat the point.
    # ------------------------------------------------------------------

    _MEDIA_BLOCK_TYPES = {"image", "audio", "video", "file"}

    # ------------------------------------------------------------------
    # Plan gate: block non-create_plan tools when /plan gate is active
    # ------------------------------------------------------------------

    _PLAN_TOOLS_WITH_JSON_ARGS = frozenset(
        {
            "create_plan",
            "revise_current_plan",
        },
    )
    _PLAN_JSON_KEYS = ("subtask", "subtasks")

    @staticmethod
    def _fix_stringified_json_args(tool_call) -> None:
        """Parse JSON-string arguments that models sometimes produce for
        nested objects (e.g. ``subtask``).  Modifies *tool_call* in place."""
        import json as _json

        inp = tool_call.get("input")
        if not isinstance(inp, dict):
            return
        for key in QwenPawAgent._PLAN_JSON_KEYS:
            val = inp.get(key)
            if isinstance(val, str):
                try:
                    inp[key] = _json.loads(val)
                except (ValueError, TypeError):
                    pass
            elif isinstance(val, list):
                for i, item in enumerate(val):
                    if isinstance(item, str):
                        try:
                            val[i] = _json.loads(item)
                        except (ValueError, TypeError):
                            pass

    async def _acting(self, tool_call) -> dict | None:
        """Check plan tool gate before delegating to ToolGuardMixin."""
        from ..plan.hints import check_plan_tool_gate
        from ..observability.langfuse import tool_span

        tool_name = str(tool_call.get("name", ""))

        async with tool_span(
            name=tool_name or "unknown",
            input=tool_call.get("input"),
            metadata={"tool_call_id": tool_call.get("id")},
        ) as langfuse_span:
            if tool_name in self._PLAN_TOOLS_WITH_JSON_ARGS:
                self._fix_stringified_json_args(tool_call)

            nb = getattr(self, "plan_notebook", None)

            # Pre-lock BEFORE executing create_plan / revise_current_plan so
            # that parallel tool calls (asyncio.gather) cannot slip an
            # execution tool past the gate before the lock is set.
            # pylint: disable=protected-access
            if nb is not None and tool_name in {
                "create_plan",
                "revise_current_plan",
            }:
                nb._plan_awaiting_user_confirm = True

            if nb is not None:
                err = check_plan_tool_gate(nb, tool_name)
                if err:
                    from agentscope.message import ToolResultBlock

                    tool_res_msg = Msg(
                        "system",
                        [
                            ToolResultBlock(
                                type="tool_result",
                                id=tool_call["id"],
                                name=tool_name,
                                output=[{"type": "text", "text": err}],
                            ),
                        ],
                        "system",
                    )
                    await self.print(tool_res_msg, True)
                    await self.memory.add(tool_res_msg)
                    if langfuse_span is not None:
                        langfuse_span.update(output={"blocked": err})
                    return None

            result = await super()._acting(tool_call)

            if langfuse_span is not None:
                langfuse_span.update(output=result)

            if nb is not None and tool_name in {
                "create_plan",
                "revise_current_plan",
            }:
                # Force the next post-plan reasoning pass to be text-only.
                # This prevents models from emitting other tools in the same
                # turn run before the user has confirmed the plan or modified
                # it.
                # pylint: disable=protected-access
                nb._plan_text_only_after_mutation = True

            return result

    _AUTO_CONTINUE_MAX_EXTRA = 2
    _AUTO_CONTINUE_TAIL_CHARS = 600

    _AUTO_CONTINUE_HINT_EN = (
        "<system-hint>"
        "Your previous assistant turn had text only (no tool calls). "
        "Use the trailing excerpt in <previous-assistant-tail> (if present) "
        "plus the conversation to decide in this **reasoning** step: if the "
        "user's task still needs tools, emit tool_use now; if it is fully "
        "done, reply with a short text only (no tools). "
        "Do not stop with plans or code fences alone when tools are still "
        "needed."
        "</system-hint>"
    )
    _AUTO_CONTINUE_HINT_ZH = (
        "<system-hint>"
        "上轮助手仅文字、未调工具。请结合上下文与 <previous-assistant-tail> "
        "（若有）在本轮推理中判断：仍需执行则立刻 tool；已完结则简短收尾。"
        "需要操作时勿只输出计划或代码块。"
        "</system-hint>"
    )

    def _auto_continue_system_hint(self) -> str:
        """Pick hint by agent language (zh vs others)."""
        raw_lang = getattr(self._agent_config, "language", None)
        lang = (raw_lang or "").strip().lower()
        if lang == "zh":
            return self._AUTO_CONTINUE_HINT_ZH
        return self._AUTO_CONTINUE_HINT_EN

    @staticmethod
    def _auto_continue_tail_context(msg: Msg, max_chars: int) -> str:
        """Assistant text suffix for hint (fixed cut, not sentence NLP)."""
        raw = msg.get_text_content() if msg is not None else ""
        text = (raw or "").strip()
        if not text:
            return ""
        if len(text) <= max_chars:
            return text
        return text[-max_chars:].lstrip()

    async def _auto_continue_if_text_only(
        self,
        msg: Msg,
        tool_choice: Literal["auto", "none", "required"] | None,
    ) -> Msg:
        """Nudge the model when it returns text-only mid-task.

        Injects a language-matched hint (with a trailing excerpt of the
        assistant text for self-review) and runs up to
        ``_AUTO_CONTINUE_MAX_EXTRA`` extra ``_reasoning`` passes until a
        tool_use appears or the cap is
        hit.  Uses the original ``tool_choice`` unchanged (no switching).
        If an extra pass still returns text-only, keep the prior response to
        avoid repeated duplicated answers.
        """
        from ..plan.hints import should_skip_auto_continue

        nb = getattr(self, "plan_notebook", None)
        if should_skip_auto_continue(nb):
            return msg

        running = self._agent_config.running
        if not running.auto_continue_on_text_only:
            return msg
        if msg is None or msg.has_content_blocks("tool_use"):
            return msg

        extra = 0
        while extra < self._AUTO_CONTINUE_MAX_EXTRA:
            if msg.has_content_blocks("tool_use"):
                break
            extra += 1
            tail = self._auto_continue_tail_context(
                msg,
                self._AUTO_CONTINUE_TAIL_CHARS,
            )
            hint_body = self._auto_continue_system_hint()
            if tail:
                hint_body += (
                    "\n\n<previous-assistant-tail>\n"
                    f"{tail}\n"
                    "</previous-assistant-tail>"
                )
            logger.info(
                "Auto-continue: text-only (%d/%d); hint + _reasoning "
                "tool_choice=%r",
                extra,
                self._AUTO_CONTINUE_MAX_EXTRA,
                tool_choice,
            )
            hint_msg = Msg("user", hint_body, "user")
            await self.memory.add(hint_msg, marks=_MemoryMark.HINT)
            try:
                next_msg = await super()._reasoning(tool_choice=tool_choice)
            except Exception:
                logger.warning(
                    "Auto-continue extra _reasoning failed; "
                    "keeping prior response",
                    exc_info=True,
                )
                break
            if next_msg.has_content_blocks("tool_use"):
                msg = next_msg
                continue
            logger.info(
                "Auto-continue extra _reasoning still text-only; "
                "keeping prior response",
            )
            break

        return msg

    def _get_model_key(self) -> str | None:
        """Return the capability-cache key for the active model."""
        model = getattr(self, "model", None)
        return getattr(model, "model_key", None)

    def _model_rejects_media(self) -> bool:
        """Check the capability cache for a learned ``rejects_media`` flag."""
        key = self._get_model_key()
        if key is None:
            return False
        return get_capability_cache().get(key, "rejects_media", False)

    def _proactive_strip_media_blocks(self) -> int:
        """Proactively strip media blocks from memory before model call.

        Only called when the active model does not support multimodal.
        Returns the number of blocks stripped.
        """
        return self._strip_media_blocks_from_memory()

    def _uses_request_time_media_normalization(self) -> bool:
        """Return True when request-time normalization can handle media."""
        return getattr(self, "formatter", None) is not None

    def _set_formatter_media_strip(self, enabled: bool) -> None:
        """Toggle request-time media stripping on the active formatter."""
        formatter = getattr(self, "formatter", None)
        if formatter is None:
            return
        setattr(formatter, "_qwenpaw_force_strip_media", enabled)

    @staticmethod
    def _filter_plan_tools(msg: Msg, nb: Any) -> Msg:
        """Arm `_plan_awaiting_user_confirm` before any tool runs.

        Race-prevention: when the assistant message carries `create_plan` /
        `revise_current_plan` alongside other ``tool_use`` blocks, callers
        of ``asyncio.gather`` may hit `_acting()`` on sibling tools before
        the mutation tool executes. Setting the lock here (before tools run)
        makes `check_plan_tool_gate` refuse non-plan-management tools while
        still returning a readable tool_result instead of stripping blocks.
        """
        if nb is None or not isinstance(msg.content, list):
            return msg
        mut = ("create_plan", "revise_current_plan")
        if any(
            isinstance(b, dict)
            and b.get("type") == "tool_use"
            and b.get("name", "") in mut
            for b in msg.content
        ):
            # pylint: disable-next=protected-access
            nb._plan_awaiting_user_confirm = True
        return msg

    # pylint: disable=too-many-branches
    async def _reasoning(
        self,
        tool_choice: Literal["auto", "none", "required"] | None = None,
    ) -> Msg:
        """Override reasoning with proactive media filtering.

        1. Proactive layer: if the model does not support
           multimodal **or** the capability cache records a previous
           ``rejects_media`` finding, strip media blocks *before* calling.
        2. Passive layer: if the model call still fails with a
           bad-request / media error, strip remaining blocks and retry,
           then record the finding in the capability cache.
        3. If the model IS marked as multimodal but still errors on
           media, log a warning about possibly inaccurate capability flag.
        4. Plan gate: `_filter_plan_tools` pre-locks when the assistant
           schedules plan mutation tools; `_plan_text_only_after_mutation`
           forces ``tool_choice="none"`` once so the model cannot issue
           execution tools immediately after ``create_plan`` / revise.
        5. Runtime simple text fast mode registers no tools and omits
           ``tool_choice`` so OpenAI-compatible providers do not reject an
           empty ``tools`` request.

        Calls ``super()._reasoning`` to keep the ToolGuardMixin
        interception active.
        """
        simple_text_fast = self._runtime_simple_text_fast_enabled()
        if simple_text_fast:
            tool_choice = None
        nb = getattr(self, "plan_notebook", None)
        if nb is not None and getattr(
            nb,
            "_plan_text_only_after_mutation",
            False,
        ):
            # pylint: disable=protected-access
            nb._plan_text_only_after_mutation = False
            tool_choice = "none"

        # --- Proactive filtering layer ---
        should_strip = (
            not get_active_model_supports_multimodal()
            or self._model_rejects_media()
        )
        if should_strip:
            if self._uses_request_time_media_normalization():
                self._set_formatter_media_strip(True)
                logger.debug(
                    "Formatter will strip media from copied messages "
                    "before reasoning.",
                )
            else:
                n = self._proactive_strip_media_blocks()
                if n > 0:
                    logger.warning(
                        "Proactively stripped %d media block(s) - "
                        "model does not support multimodal.",
                        n,
                    )

        disable_runtime_thinking = (
            simple_text_fast
            and _runtime_disable_thinking_from_context(self._request_context)
        )

        async def _call_parent_reasoning() -> Msg:
            if disable_runtime_thinking:
                with _temporarily_disable_model_thinking(
                    getattr(self, "model", None),
                ):
                    return await super(QwenPawAgent, self)._reasoning(
                        tool_choice=tool_choice,
                    )
            return await super(QwenPawAgent, self)._reasoning(
                tool_choice=tool_choice,
            )

        # --- Passive fallback layer (existing logic) ---
        try:
            msg = await _call_parent_reasoning()
        except Exception as e:
            if not self._is_bad_request_or_media_error(e):
                raise

            model_key = self._get_model_key()

            if self._uses_request_time_media_normalization():
                if get_active_model_supports_multimodal():
                    logger.warning(
                        "Model marked multimodal but "
                        "rejected media. "
                        "Capability flag may be wrong.",
                    )
                self._set_formatter_media_strip(True)
                try:
                    logger.warning(
                        "_reasoning failed (%s). "
                        "Retrying with request-time media stripping.",
                        e,
                    )
                    msg = await _call_parent_reasoning()
                    if model_key:
                        get_capability_cache().learn(
                            model_key,
                            "rejects_media",
                            True,
                        )
                    return msg
                finally:
                    self._set_formatter_media_strip(False)

            n_stripped = self._strip_media_blocks_from_memory()
            if n_stripped == 0:
                raise

            if get_active_model_supports_multimodal():
                logger.warning(
                    "Model marked multimodal but "
                    "rejected media. "
                    "Capability flag may be wrong.",
                )

            logger.warning(
                "_reasoning failed (%s). "
                "Stripped %d media block(s) from memory, retrying.",
                e,
                n_stripped,
            )
            msg = await _call_parent_reasoning()
            if model_key:
                get_capability_cache().learn(
                    model_key,
                    "rejects_media",
                    True,
                )
        finally:
            if should_strip and self._uses_request_time_media_normalization():
                self._set_formatter_media_strip(False)

        msg = self._filter_plan_tools(msg, nb)

        if simple_text_fast:
            return msg
        return await self._auto_continue_if_text_only(msg, tool_choice)

    # pylint: disable=too-many-branches
    async def _summarizing(self) -> Msg:
        """Override summarizing with proactive media filtering,
        passive fallback, and tool_use block filtering.

        1. Proactive layer: if the model does not support multimodal
           **or** the capability cache records ``rejects_media``,
           strip media blocks *before* calling the model.
        2. Passive layer: if the model call still fails with a
           bad-request / media error, strip remaining blocks and retry,
           then record the finding in the capability cache.
        3. If the model IS marked as multimodal but still errors on
           media, log a warning about possibly inaccurate capability flag.

        Some models (e.g. kimi-k2.5) generate tool_use blocks even when
        no tools are provided.  We set ``_in_summarizing`` so that
        ``print`` can strip tool_use blocks from streaming chunks.
        """
        # --- Proactive filtering layer ---
        should_strip = (
            not get_active_model_supports_multimodal()
            or self._model_rejects_media()
        )
        if should_strip:
            if self._uses_request_time_media_normalization():
                self._set_formatter_media_strip(True)
                logger.debug(
                    "Formatter will strip media from copied messages "
                    "before summarizing.",
                )
            else:
                n = self._proactive_strip_media_blocks()
                if n > 0:
                    logger.warning(
                        "Proactively stripped %d media block(s) - "
                        "model does not support multimodal.",
                        n,
                    )

        # --- Passive fallback layer ---
        self._in_summarizing = True
        try:
            try:
                msg = await super()._summarizing()
            except Exception as e:
                if not self._is_bad_request_or_media_error(e):
                    raise

                model_key = self._get_model_key()

                if self._uses_request_time_media_normalization():
                    if get_active_model_supports_multimodal():
                        logger.warning(
                            "Model marked multimodal but "
                            "rejected media. "
                            "Capability flag may be wrong.",
                        )
                    self._set_formatter_media_strip(True)
                    try:
                        logger.warning(
                            "_summarizing failed (%s). "
                            "Retrying with request-time media stripping.",
                            e,
                        )
                        msg = await super()._summarizing()
                        if model_key:
                            get_capability_cache().learn(
                                model_key,
                                "rejects_media",
                                True,
                            )
                    finally:
                        self._set_formatter_media_strip(False)
                else:
                    n_stripped = self._strip_media_blocks_from_memory()
                    if n_stripped == 0:
                        raise

                    if get_active_model_supports_multimodal():
                        logger.warning(
                            "Model marked multimodal but "
                            "rejected media. "
                            "Capability flag may be wrong.",
                        )

                    logger.warning(
                        "_summarizing failed (%s). "
                        "Stripped %d media block(s) from memory, retrying.",
                        e,
                        n_stripped,
                    )
                    msg = await super()._summarizing()
                    if model_key:
                        get_capability_cache().learn(
                            model_key,
                            "rejects_media",
                            True,
                        )
        finally:
            self._in_summarizing = False
            if should_strip and self._uses_request_time_media_normalization():
                self._set_formatter_media_strip(False)

        return self._strip_tool_use_from_msg(msg)

    async def print(
        self,
        msg: Msg,
        last: bool = True,
        speech: Any = None,
    ) -> None:
        """Filter tool_use blocks during _summarizing before they hit the
        message queue, preventing the frontend from briefly rendering
        phantom tool calls that will never be executed.

        On the *final* streaming event (``last=True``), append the
        round-end notice so users see it immediately instead of only
        after a page refresh.  Intermediate events that become empty
        after filtering are silently skipped to avoid blank UI flashes.
        """

        if not getattr(self, "_in_summarizing", False):
            return await super().print(msg, last, speech=speech)

        original = msg.content
        modified = False

        if isinstance(original, list):
            filtered = [
                b
                for b in original
                if not (isinstance(b, dict) and b.get("type") == "tool_use")
            ]
            if not filtered and not last:
                return
            if len(filtered) != len(original) or last:
                msg.content = filtered
                if last:
                    msg.content.append(
                        {"type": "text", "text": self._ROUND_END_NOTICE},
                    )
                modified = True
        elif isinstance(original, str) and last:
            msg.content = original + self._ROUND_END_NOTICE
            modified = True
        if modified:
            try:
                return await super().print(msg, last, speech=speech)
            finally:
                msg.content = original
        return await super().print(msg, last, speech=speech)

    _ROUND_END_NOTICE = (
        "\n\n---\n"
        "本轮调用已达最大次数，回复已终止，请继续输入。\n"
        "Maximum iterations reached for this round. "
        "Please send a new message to continue."
    )

    @staticmethod
    def _strip_tool_use_from_msg(msg: Msg) -> Msg:
        """Remove tool_use blocks from a message and append a user notice.

        When _summarizing is called without tools, some models still
        return tool_use blocks.  Those blocks can never be executed, so
        strip them and append a bilingual notice telling the user this
        round of calls has ended.
        """
        if isinstance(msg.content, str):
            msg.content += QwenPawAgent._ROUND_END_NOTICE
            return msg

        filtered = [
            block
            for block in msg.content
            if not (
                isinstance(block, dict) and block.get("type") == "tool_use"
            )
        ]

        n_removed = len(msg.content) - len(filtered)
        if n_removed:
            logger.debug(
                "Stripped %d tool_use block(s) from _summarizing response",
                n_removed,
            )

        filtered.append(
            {"type": "text", "text": QwenPawAgent._ROUND_END_NOTICE},
        )
        msg.content = filtered
        return msg

    @staticmethod
    def _is_bad_request_or_media_error(exc: Exception) -> bool:
        """Return True for 400-class or media-related model errors.

        Targets bad-request (400) errors because unsupported media
        content typically causes request validation failures.  Keyword
        matching provides an extra safety net for providers that use
        non-standard status codes.
        """
        status = getattr(exc, "status_code", None)
        if status == 400:
            return True

        error_str = str(exc).lower()
        keywords = [
            "image",
            "audio",
            "video",
            "vision",
            "multimodal",
            "image_url",
        ]
        return any(kw in error_str for kw in keywords)

    def _strip_media_blocks_from_memory(self) -> int:
        """Remove media blocks (image/audio/video) from all messages.

        Also strips media blocks nested inside ToolResultBlock outputs.
        Inserts placeholder text when stripping leaves content empty to
        avoid malformed API requests.

        Returns:
            Total number of media blocks removed.
        """
        media_types = self._MEDIA_BLOCK_TYPES
        total_stripped = 0

        for msg, _marks in self.memory.content:
            if not isinstance(msg.content, list):
                continue

            new_content = []
            stripped_this_message = 0
            for block in msg.content:
                if (
                    isinstance(block, dict)
                    and block.get("type") in media_types
                ):
                    total_stripped += 1
                    stripped_this_message += 1
                    continue

                if (
                    isinstance(block, dict)
                    and block.get("type") == "tool_result"
                    and isinstance(block.get("output"), list)
                ):
                    original_len = len(block["output"])
                    block["output"] = [
                        item
                        for item in block["output"]
                        if not (
                            isinstance(item, dict)
                            and item.get("type") in media_types
                        )
                    ]
                    stripped_count = original_len - len(block["output"])
                    total_stripped += stripped_count
                    stripped_this_message += stripped_count
                    if stripped_count > 0 and not block["output"]:
                        block["output"] = MEDIA_UNSUPPORTED_PLACEHOLDER

                new_content.append(block)

            if not new_content and stripped_this_message > 0:
                new_content.append(
                    {
                        "type": "text",
                        "text": MEDIA_UNSUPPORTED_PLACEHOLDER,
                    },
                )

            msg.content = new_content

        return total_stripped

    # pylint: disable=protected-access
    async def reply(
        self,
        msg: Msg | list[Msg] | None = None,
        structured_model: Type[BaseModel] | None = None,
    ) -> Msg:
        """Run one reply with an isolated supplemental-file discovery set."""
        from ..config.context import (
            reset_current_runtime_discovered_file_ids,
            set_current_runtime_discovered_file_ids,
        )

        discovered_token = set_current_runtime_discovered_file_ids(
            frozenset(),
        )
        try:
            return await self._reply_with_request_context(
                msg=msg,
                structured_model=structured_model,
            )
        finally:
            reset_current_runtime_discovered_file_ids(discovered_token)

    async def _reply_with_request_context(
        self,
        msg: Msg | list[Msg] | None = None,
        structured_model: Type[BaseModel] | None = None,
    ) -> Msg:
        """Override reply to process file blocks and handle commands.

        Args:
            msg: Input message(s) from user
            structured_model: Optional pydantic model for structured output

        Returns:
            Response message
        """
        # Set workspace_dir and recent_max_bytes in context for tool functions
        from ..config.context import (
            set_current_workspace_dir,
            set_current_recent_max_bytes,
            set_current_session_id,
            set_current_runtime_attachments_manifest,
            set_current_runtime_sandbox_context,
            set_current_runtime_tool_gateway,
            set_current_shell_command_timeout,
            set_current_shell_command_executable,
            set_current_toolkit,
        )

        set_current_workspace_dir(self._workspace_dir)
        set_current_toolkit(getattr(self, "toolkit", None))
        set_current_session_id(
            self._request_context.get("session_id") or None,
        )
        gateway = self._request_context.get("runtime_tool_gateway")
        if isinstance(gateway, dict):
            gateway = dict(gateway)
            for key in ("trace_id",):
                if self._request_context.get(key) and not gateway.get(key):
                    gateway[key] = self._request_context[key]
            set_current_runtime_tool_gateway(gateway)
        else:
            set_current_runtime_tool_gateway(None)
        manifest = self._request_context.get("attachments_manifest")
        set_current_runtime_attachments_manifest(
            manifest if isinstance(manifest, list) else None,
        )
        sandbox_context = self._request_context.get("sandbox_context")
        set_current_runtime_sandbox_context(
            sandbox_context if isinstance(sandbox_context, dict) else None,
        )
        light_ctx = self._agent_config.running.light_context_config
        pruning_config = light_ctx.tool_result_pruning_config
        set_current_recent_max_bytes(
            pruning_config.pruning_recent_msg_max_bytes,
        )
        set_current_shell_command_timeout(
            self._agent_config.running.shell_command_timeout,
        )
        set_current_shell_command_executable(
            self._agent_config.running.shell_command_executable or None,
        )

        # Process file and media blocks in messages
        if msg is not None:
            msg = await _append_runtime_attachment_content_parts(
                msg,
                self._request_context,
            )
            await process_file_and_media_blocks_in_message(msg)

        # Check if message is a system command
        last_msg = msg[-1] if isinstance(msg, list) else msg
        query = (
            last_msg.get_text_content() if isinstance(last_msg, Msg) else None
        )

        if self.command_handler.is_command(query):
            logger.info(f"Received command: {query}")
            msg = await self.command_handler.handle_command(query)
            await self.print(msg)
            return msg

        # Normal message processing
        logger.info("QwenPawAgent.reply: max_iters=%s", self.max_iters)

        if self._runtime_simple_text_fast_enabled():
            return await super().reply(
                msg=msg,
                structured_model=structured_model,
            )

        request_context = getattr(self, "_request_context", {}) or {}
        channel_name = request_context.get("channel", "console")
        workspace_dir = Path(self._workspace_dir or WORKING_DIR)
        with apply_skill_config_env_overrides(workspace_dir, channel_name):
            return await super().reply(
                msg=msg,
                structured_model=structured_model,
            )

    async def interrupt(self, msg: Msg | list[Msg] | None = None) -> None:
        """Interrupt the current reply process and wait for cleanup."""
        if self._reply_task and not self._reply_task.done():
            task = self._reply_task
            task.cancel(msg)
            try:
                await task
            except asyncio.CancelledError:
                if not task.cancelled():
                    raise
            except Exception:
                logger.warning(
                    "Exception occurred during interrupt cleanup",
                    exc_info=True,
                )

    async def _broadcast_to_subscribers(self, msg):
        # agentscope hook wrapper may misidentify an
        # async bound method as sync, returning an
        # unawaited coroutine instead of a Msg.
        if inspect.iscoroutine(msg):
            msg = await msg
        elif isinstance(msg, list):
            msg = [(await m) if inspect.iscoroutine(m) else m for m in msg]
        await super()._broadcast_to_subscribers(msg)
