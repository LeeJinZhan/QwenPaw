"""Task-scoped visibility filtering for QwenPaw Driver tools."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import re
from typing import Any

from qwenpaw.drivers.adapters.agentscope_tool import DriverCapabilityTool

_WORKER_TOOL_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,127}")
_SNAPSHOT_HASH = re.compile(r"sha256:[0-9a-f]{64}")


@dataclass(frozen=True)
class RuntimeToolVisibilityProjection:
    worker_type: str
    worker_tool_names: frozenset[str]
    binding_snapshot_hash: str
    authoritative: bool = False


def parse_runtime_tool_visibility(
    value: Any,
) -> RuntimeToolVisibilityProjection | None:
    if not isinstance(value, Mapping):
        return None
    worker_type = str(value.get("worker_type") or "").strip()
    names_value = value.get("worker_tool_names")
    snapshot_hash = str(value.get("binding_snapshot_hash") or "").strip().lower()
    if (
        not worker_type
        or not isinstance(names_value, list)
        or value.get("authoritative") is not False
        or not _SNAPSHOT_HASH.fullmatch(snapshot_hash)
    ):
        return None
    names = [str(item).strip() for item in names_value]
    if len(names) != len(set(names)) or any(
        not name or not _WORKER_TOOL_NAME.fullmatch(name) for name in names
    ):
        return None
    return RuntimeToolVisibilityProjection(
        worker_type=worker_type,
        worker_tool_names=frozenset(names),
        binding_snapshot_hash=snapshot_hash,
    )


def filter_managed_driver_tools(toolkit: Any, allowed_names: frozenset[str]) -> None:
    groups = getattr(toolkit, "tool_groups", None)
    if not isinstance(groups, list):
        raise RuntimeError("Runtime managed tool registry is unavailable")
    for group in groups:
        tools = getattr(group, "tools", None)
        if not isinstance(tools, list):
            raise RuntimeError("Runtime managed tool group is unavailable")
        tools[:] = [
            tool
            for tool in tools
            if not isinstance(tool, DriverCapabilityTool)
            or str(getattr(tool, "name", "") or "") in allowed_names
        ]


__all__ = [
    "RuntimeToolVisibilityProjection",
    "filter_managed_driver_tools",
    "parse_runtime_tool_visibility",
]
