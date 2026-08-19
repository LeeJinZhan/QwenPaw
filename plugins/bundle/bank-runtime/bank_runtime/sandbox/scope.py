"""Strict request-local file candidate scope with no storage locators."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Mapping

_ID = re.compile(r"^[A-Za-z0-9_-]{1,160}$")
_MAX_MANIFEST_FILES = 20
_MAX_SELECTED_FILES = 3
_PUBLIC_FIELDS = (
    "file_id",
    "display_name",
    "content_type",
    "size_bytes",
    "created_at",
    "source",
    "readable",
    "status_label",
)
_DISCOVERED_SOURCES = frozenset({"conversation", "assistant_workspace"})


class SandboxScopeError(ValueError):
    """Sandbox request metadata is malformed or outside this request."""


@dataclass
class SandboxRequestScope:
    task_id: str
    sandbox_context: dict[str, Any]
    attachments_manifest: tuple[dict[str, Any], ...]
    current_attachment_ids: tuple[str, ...]
    discovered_files: dict[str, dict[str, Any]] = field(default_factory=dict)
    selected_file_ids: set[str] = field(default_factory=set)

    @classmethod
    def from_request(cls, request: Any) -> "SandboxRequestScope":
        task_id = _safe_id(getattr(request, "runtime_task_id", None), "task_id")
        context = getattr(request, "sandbox_context", None)
        if not isinstance(context, Mapping):
            raise SandboxScopeError("Runtime sandbox context is required")
        context = dict(context)
        if _safe_id(context.get("task_id"), "sandbox task_id") != task_id:
            raise SandboxScopeError("Runtime sandbox task scope mismatch")
        if not str(context.get("context_id") or "").strip():
            raise SandboxScopeError("Runtime sandbox context id is required")
        if not str(context.get("signature") or "").strip():
            raise SandboxScopeError("Runtime sandbox signature is required")

        raw_manifest = getattr(request, "attachments_manifest", None)
        if raw_manifest is None:
            raw_manifest = []
        if (
            not isinstance(raw_manifest, list)
            or len(raw_manifest) > _MAX_MANIFEST_FILES
        ):
            raise SandboxScopeError("Runtime attachment manifest is invalid")
        manifest: list[dict[str, Any]] = []
        current_ids: list[str] = []
        seen: set[str] = set()
        for raw in raw_manifest:
            if not isinstance(raw, Mapping):
                raise SandboxScopeError("Runtime attachment manifest is invalid")
            file_id = _safe_id(raw.get("file_id"), "file_id")
            if file_id in seen:
                raise SandboxScopeError("Runtime attachment manifest has duplicates")
            seen.add(file_id)
            source = str(raw.get("source") or "current_task").strip()
            if source != "current_task":
                raise SandboxScopeError(
                    "Runtime request attachments must be current-task"
                )
            item = _public_file(raw, default_source="current_task", readable=True)
            manifest.append(item)
            current_ids.append(file_id)
        return cls(
            task_id=task_id,
            sandbox_context=context,
            attachments_manifest=tuple(manifest),
            current_attachment_ids=tuple(current_ids),
        )

    def remember_discovered(self, files: Any) -> None:
        if not isinstance(files, list):
            raise SandboxScopeError("Runtime file search response is invalid")
        for raw in files:
            if not isinstance(raw, Mapping):
                continue
            try:
                item = _public_file(raw)
            except SandboxScopeError:
                continue
            if item["source"] not in _DISCOVERED_SOURCES:
                continue
            if item["readable"] is not True:
                continue
            if item["file_id"] in self.current_attachment_ids:
                continue
            self.discovered_files[item["file_id"]] = item

    def selection_records(self, file_ids: Any) -> list[dict[str, str]]:
        if not isinstance(file_ids, list) or not file_ids:
            raise SandboxScopeError("Runtime file selection requires file IDs")
        normalized = [_safe_id(value, "file_id") for value in file_ids]
        if len(self.selected_file_ids.union(normalized)) > _MAX_SELECTED_FILES:
            raise SandboxScopeError("Runtime file selection limit exceeded")
        if len(set(normalized)) != len(normalized):
            raise SandboxScopeError("Runtime file selection has duplicates")
        records: list[dict[str, str]] = []
        for file_id in normalized:
            item = self.discovered_files.get(file_id)
            if item is None:
                raise SandboxScopeError(
                    "Runtime file was not discovered in this request"
                )
            records.append(
                {
                    "file_id": file_id,
                    "source": str(item["source"]),
                    "selection_mode": "model_metadata_selection",
                }
            )
        return records

    def mark_selected(self, file_ids: list[str]) -> None:
        normalized = {_safe_id(value, "file_id") for value in file_ids}
        if len(self.selected_file_ids.union(normalized)) > _MAX_SELECTED_FILES:
            raise SandboxScopeError("Runtime file selection limit exceeded")
        self.selected_file_ids.update(normalized)


def _safe_id(value: Any, field: str) -> str:
    normalized = str(value or "").strip()
    if not _ID.fullmatch(normalized):
        raise SandboxScopeError(f"Runtime sandbox {field} is invalid")
    return normalized


def _public_file(
    raw: Mapping[str, Any],
    *,
    default_source: str = "",
    readable: bool | None = None,
) -> dict[str, Any]:
    file_id = _safe_id(raw.get("file_id"), "file_id")
    item = {field: raw.get(field) for field in _PUBLIC_FIELDS}
    item["file_id"] = file_id
    item["display_name"] = str(
        item.get("display_name") or raw.get("original_name") or ""
    )[:255]
    item["content_type"] = str(item.get("content_type") or "")[:128]
    item["created_at"] = str(item.get("created_at") or "")[:64]
    item["source"] = str(item.get("source") or default_source)[:64]
    item["status_label"] = str(item.get("status_label") or "")[:64]
    item["readable"] = (
        bool(readable) if readable is not None else item.get("readable") is True
    )
    try:
        item["size_bytes"] = max(int(item.get("size_bytes") or 0), 0)
    except (TypeError, ValueError):
        item["size_bytes"] = 0
    return item


__all__ = ["SandboxRequestScope", "SandboxScopeError"]
