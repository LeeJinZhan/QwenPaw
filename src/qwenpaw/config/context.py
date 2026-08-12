# -*- coding: utf-8 -*-
"""Context variable for agent workspace directory.

This module provides a context variable to pass the agent's workspace
directory to tool functions, allowing them to resolve relative paths
correctly in a multi-agent environment.
"""
from __future__ import annotations

from contextvars import ContextVar, Token
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agentscope.tool import Toolkit

# Context variable to store the current agent's workspace directory
current_workspace_dir: ContextVar[Path | None] = ContextVar(
    "current_workspace_dir",
    default=None,
)


def get_current_workspace_dir() -> Path | None:
    """Get the current agent's workspace directory from context.

    Returns:
        Path to the current agent's workspace directory, or None if not set.
    """
    return current_workspace_dir.get()


def set_current_workspace_dir(workspace_dir: Path | None) -> Token:
    """Set the current agent's workspace directory in context.

    Args:
        workspace_dir: Path to the agent's workspace directory.
    """
    return current_workspace_dir.set(workspace_dir)


def reset_current_workspace_dir(token: Token) -> None:
    """Restore the workspace directory from before the current request."""
    current_workspace_dir.reset(token)


current_file_sandbox_root: ContextVar[Path | None] = ContextVar(
    "current_file_sandbox_root",
    default=None,
)


def get_current_file_sandbox_root() -> Path | None:
    """Return the request-local root enforced by generic file tools."""
    return current_file_sandbox_root.get()


def set_current_file_sandbox_root(
    sandbox_root: Path | None,
) -> Token:
    """Set the request-local file sandbox root and return a reset token."""
    return current_file_sandbox_root.set(sandbox_root)


def reset_current_file_sandbox_root(token: Token) -> None:
    """Restore the file sandbox root from before the current request."""
    current_file_sandbox_root.reset(token)


# Context variable to store the recent_max_bytes limit
current_recent_max_bytes: ContextVar[int | None] = ContextVar(
    "current_recent_max_bytes",
    default=None,
)


def get_current_recent_max_bytes() -> int | None:
    """Get the current agent's recent_max_bytes limit from context.

    Returns:
        Byte limit for recent tool output truncation, or None if not set.
    """
    return current_recent_max_bytes.get()


def set_current_recent_max_bytes(max_bytes: int | None) -> None:
    """Set the current agent's recent_max_bytes limit in context.

    Args:
        max_bytes: Byte limit for recent tool output truncation.
    """
    current_recent_max_bytes.set(max_bytes)


# Context variable to store the configured shell command timeout
current_shell_command_timeout: ContextVar[float | None] = ContextVar(
    "current_shell_command_timeout",
    default=None,
)


def get_current_shell_command_timeout() -> float | None:
    """Get the configured default timeout for execute_shell_command.

    Returns:
        Timeout in seconds, or None if not configured.
    """
    return current_shell_command_timeout.get()


def set_current_shell_command_timeout(timeout: float | None) -> None:
    """Set the configured default timeout for execute_shell_command.

    Args:
        timeout: Timeout in seconds.
    """
    current_shell_command_timeout.set(timeout)


current_shell_command_executable: ContextVar[str | None] = ContextVar(
    "current_shell_command_executable",
    default=None,
)


def get_current_shell_command_executable() -> str | None:
    """Get the configured shell executable for execute_shell_command.

    Returns:
        Path to the shell executable, or None if not configured.
    """
    return current_shell_command_executable.get()


def set_current_shell_command_executable(executable: str | None) -> None:
    """Set the configured shell executable for execute_shell_command.

    Args:
        executable: Path to the shell executable (e.g. "/bin/bash").
    """
    current_shell_command_executable.set(executable)


# Context variable to store the current session ID for tool functions
current_session_id: ContextVar[str | None] = ContextVar(
    "current_session_id",
    default=None,
)


def get_current_session_id() -> str | None:
    """Get the current session ID from context.

    Returns:
        Current session ID, or None if not set.
    """
    return current_session_id.get()


def set_current_session_id(session_id: str | None) -> None:
    """Set the current session ID in context.

    Args:
        session_id: Session ID to store in context.
    """
    current_session_id.set(session_id)


# Context variable to store the current agent's Toolkit instance
current_toolkit: ContextVar[Toolkit | None] = ContextVar(
    "current_toolkit",
    default=None,
)


def get_current_toolkit() -> Toolkit | None:
    """Get the current agent's Toolkit instance from context.

    Returns:
        The current Toolkit instance, or None if not set.
    """
    return current_toolkit.get()


def set_current_toolkit(toolkit: Toolkit | None) -> None:
    """Set the current agent's Toolkit instance in context.

    Args:
        toolkit: Toolkit instance to store in context.
    """
    current_toolkit.set(toolkit)


current_runtime_tool_gateway: ContextVar[dict[str, Any] | None] = ContextVar(
    "current_runtime_tool_gateway",
    default=None,
)


def get_current_runtime_tool_gateway() -> dict[str, Any] | None:
    """Get Runtime Tool Gateway metadata for the current tool call."""
    return current_runtime_tool_gateway.get()


def set_current_runtime_tool_gateway(
    gateway: dict[str, Any] | None,
) -> None:
    """Set Runtime Tool Gateway metadata for the current tool call."""
    current_runtime_tool_gateway.set(gateway)


current_runtime_attachments_manifest: ContextVar[list[dict[str, Any]] | None] = ContextVar(
    "current_runtime_attachments_manifest",
    default=None,
)


def get_current_runtime_attachments_manifest() -> list[dict[str, Any]] | None:
    """Get Runtime-issued attachment grants for the current request."""
    return current_runtime_attachments_manifest.get()


def set_current_runtime_attachments_manifest(
    manifest: list[dict[str, Any]] | None,
) -> None:
    """Set Runtime-issued attachment grants for the current request."""
    current_runtime_attachments_manifest.set(manifest)


current_runtime_sandbox_context: ContextVar[dict[str, Any] | None] = ContextVar(
    "current_runtime_sandbox_context",
    default=None,
)


def get_current_runtime_sandbox_context() -> dict[str, Any] | None:
    """Get Runtime-issued sandbox context for the current request."""
    return current_runtime_sandbox_context.get()


def set_current_runtime_sandbox_context(
    sandbox_context: dict[str, Any] | None,
) -> None:
    """Set Runtime-issued sandbox context for the current request."""
    current_runtime_sandbox_context.set(sandbox_context)


current_runtime_discovered_file_ids: ContextVar[frozenset[str]] = ContextVar(
    "current_runtime_discovered_file_ids",
    default=frozenset(),
)

current_runtime_discovered_files: ContextVar[dict[str, dict[str, Any]]] = ContextVar(
    "current_runtime_discovered_files",
    default={},
)

current_runtime_selected_file_ids: ContextVar[frozenset[str]] = ContextVar(
    "current_runtime_selected_file_ids",
    default=frozenset(),
)


current_runtime_tool_execution: ContextVar[dict[str, Any] | None] = ContextVar(
    "current_runtime_tool_execution",
    default=None,
)


def get_current_runtime_tool_execution() -> dict[str, Any] | None:
    """Return the permit-bound tool call currently entering a tool body."""
    value = current_runtime_tool_execution.get()
    return dict(value) if isinstance(value, dict) else None


def push_current_runtime_tool_execution(value: dict[str, Any]) -> Token:
    return current_runtime_tool_execution.set(dict(value))


def reset_current_runtime_tool_execution(token: Token) -> None:
    current_runtime_tool_execution.reset(token)


def get_current_runtime_discovered_file_ids() -> frozenset[str]:
    """Get file IDs discovered during the current Runtime request."""
    return current_runtime_discovered_file_ids.get()


def set_current_runtime_discovered_file_ids(
    file_ids: frozenset[str] | set[str] | list[str] | tuple[str, ...],
) -> Token:
    """Replace request-local discovered file IDs and return a reset token."""
    normalized = frozenset(
        str(file_id).strip()
        for file_id in file_ids
        if str(file_id).strip()
    )
    return current_runtime_discovered_file_ids.set(normalized)


def reset_current_runtime_discovered_file_ids(token: Token) -> None:
    """Restore discovered file IDs to the value before this request."""
    current_runtime_discovered_file_ids.reset(token)


def get_current_runtime_discovered_files() -> dict[str, dict[str, Any]]:
    """Return safe metadata registered by file search in this request."""
    return {
        file_id: dict(metadata)
        for file_id, metadata in current_runtime_discovered_files.get().items()
    }


def set_current_runtime_discovered_files(
    files: list[dict[str, Any]] | tuple[dict[str, Any], ...],
) -> Token:
    """Replace request-local search results and return a reset token."""
    normalized: dict[str, dict[str, Any]] = {}
    for item in files:
        if not isinstance(item, dict):
            continue
        file_id = str(item.get("file_id") or "").strip()
        if not file_id:
            continue
        normalized[file_id] = dict(item)
    return current_runtime_discovered_files.set(normalized)


def merge_current_runtime_discovered_files(
    files: list[dict[str, Any]],
) -> None:
    """Merge safe search results into the current request registry."""
    merged = get_current_runtime_discovered_files()
    for item in files:
        if not isinstance(item, dict):
            continue
        file_id = str(item.get("file_id") or "").strip()
        if file_id:
            merged[file_id] = dict(item)
    current_runtime_discovered_files.set(merged)
    current_runtime_discovered_file_ids.set(frozenset(merged))


def reset_current_runtime_discovered_files(token: Token) -> None:
    """Restore safe search metadata to its pre-request value."""
    current_runtime_discovered_files.reset(token)


def get_current_runtime_selected_file_ids() -> frozenset[str]:
    """Return files already selected autonomously in this request."""
    return current_runtime_selected_file_ids.get()


def set_current_runtime_selected_file_ids(
    file_ids: frozenset[str] | set[str] | list[str] | tuple[str, ...],
) -> Token:
    """Replace autonomously selected file IDs and return a reset token."""
    normalized = frozenset(
        str(file_id).strip()
        for file_id in file_ids
        if str(file_id).strip()
    )
    return current_runtime_selected_file_ids.set(normalized)


def reset_current_runtime_selected_file_ids(token: Token) -> None:
    """Restore selected file IDs to the pre-request value."""
    current_runtime_selected_file_ids.reset(token)


_REQUEST_LOCAL_CONTEXT_VARS: tuple[ContextVar[Any], ...] = (
    current_workspace_dir,
    current_file_sandbox_root,
    current_recent_max_bytes,
    current_shell_command_timeout,
    current_shell_command_executable,
    current_session_id,
    current_toolkit,
    current_runtime_tool_gateway,
    current_runtime_attachments_manifest,
    current_runtime_sandbox_context,
    current_runtime_discovered_file_ids,
    current_runtime_discovered_files,
    current_runtime_selected_file_ids,
    current_runtime_tool_execution,
)


def snapshot_request_local_context() -> list[tuple[ContextVar[Any], Token]]:
    """Create a reset boundary around all tool/request ContextVars.

    The values are copied into the current context solely to obtain reset
    tokens.  Any setters invoked while the request runs are then rolled back
    as a group, including legacy setters whose callers do not retain tokens.
    """
    return [(variable, variable.set(variable.get())) for variable in _REQUEST_LOCAL_CONTEXT_VARS]


def restore_request_local_context(
    snapshot: list[tuple[ContextVar[Any], Token]],
) -> None:
    """Restore a snapshot created by :func:`snapshot_request_local_context`."""
    for variable, token in reversed(snapshot):
        variable.reset(token)
