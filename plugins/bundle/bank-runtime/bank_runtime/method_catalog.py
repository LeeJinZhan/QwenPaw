"""Read-only catalog for the authenticated Runtime, scoped to its bound agent."""

import hashlib
import json
import os
import re
import stat
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace

import frontmatter
import yaml
from fastapi import APIRouter, Header, HTTPException, Request
from starlette.concurrency import run_in_threadpool
from qwenpaw.app.agent_context import get_agent_for_request
from qwenpaw.agents.skill_system.registry import resolve_effective_skills
from qwenpaw.agents.skill_system.store import get_workspace_skills_dir, read_skill_manifest

from .gateway.skill_references import read_reference


@lru_cache(maxsize=1)
def _bundled_titles():
    """Bank plugin skill titles from the installed package, keyed by native directory id."""
    titles = {}
    for doc in (Path(__file__).resolve().parents[1] / "skills").glob("*/SKILL.md"):
        with doc.open(encoding="utf-8-sig") as source:
            header = source.read(8192)
        # The package uses YAML frontmatter followed by its official Markdown title.
        body = header.partition("\n---\n")[2]
        heading = re.match(r"\s*# ([^\r\n]{1,128})(?:\n|$)", body)
        if heading:
            titles[doc.parent.name] = heading[1].strip()
    return titles


def _skill_bytes(directory, *, header_only=False):
    folder = os.open(directory, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY)
    try:
        fd = os.open("SKILL.md", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 131072:
                raise ValueError("Invalid skill document")
            if header_only:
                return stream.read(8192)
            raw = stream.read(131073)
            if len(raw) > 131072:
                raise ValueError("Skill document too large")
            return raw
    finally:
        os.close(folder)


def _material_sections(directory):
    # Directory hints come from the current file, never from a stale manifest.
    raw = _skill_bytes(directory, header_only=True).replace(b"\r\n", b"\n").removeprefix(b"\xef\xbb\xbf")
    header, separator, _ = raw.partition(b"\n---\n")
    if not separator or not header.startswith(b"---\n"):
        return []
    metadata = frontmatter.loads((header + separator).decode("utf-8")).metadata.get("metadata")
    aliases = metadata.get("background_material_sections") if isinstance(metadata, dict) else None
    if not isinstance(aliases, list) or len(aliases) > 24:
        return []
    return list(dict.fromkeys(a.strip() for a in aliases if isinstance(a, str)
                              and re.fullmatch(r"[\w -]{1,40}", a.strip()) and a.strip()))


def native_catalog(workspace):
    entries = read_skill_manifest(workspace).get("skills", {})
    root = get_workspace_skills_dir(workspace)
    items = []
    for name in resolve_effective_skills(workspace, "bank-runtime"):
        if not re.fullmatch(r"[\w.-]{1,128}", name) or name in {".", ".."}:
            continue
        directory = root / name
        document = directory / "SKILL.md"
        if directory.is_symlink() or document.is_symlink() or not document.is_file():
            continue
        metadata = entries.get(name, {}).get("metadata") or {}
        display = metadata.get("display_name")
        display = display.strip() if isinstance(display, str) else ""
        if display == name or not 0 < len(display) <= 128 or not display.isprintable():
            display = _bundled_titles().get(name, name)
        item = {"id": name, "display_name": display, "description": str(metadata.get("description") or "")[:2048]}
        try:
            sections = _material_sections(directory)
        except (OSError, ValueError, TypeError, yaml.YAMLError):
            sections = []
        if sections:
            item["background_material_sections"] = sections
        items.append(item)
    return items


def native_document(workspace, name):
    if not re.fullmatch(r"[\w.-]{1,128}", name) or name in {".", ".."}:
        raise HTTPException(404, "Skill not found")
    if not any(row["id"] == name for row in native_catalog(workspace)):
        raise HTTPException(404, "Skill not enabled for this channel")
    directory = get_workspace_skills_dir(workspace) / name
    try:
        raw = _skill_bytes(directory)
        markdown = raw.decode("utf-8-sig")
        paths = sorted(set(re.findall(r"\[[^\]\n]*\]\((references/[^\s)]+)\)", markdown)))
        if len(paths) > 32:
            raise ValueError("Too many skill references")
        skill = SimpleNamespace(markdown=markdown, dir=directory)
        references = {path: read_reference(skill, path) for path in paths}
    except (OSError, ValueError) as exc:
        raise HTTPException(409, "Skill document unavailable") from exc
    document = {"markdown": markdown, "references": references}
    digest = hashlib.sha256(
        json.dumps(document, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
    # Parse exactly the bytes hashed above. The manifest may predate an edit.
    try:
        header = frontmatter.loads(markdown).metadata.get("metadata")
    except (ValueError, TypeError, yaml.YAMLError):
        header = None
    return {
        **document, "version": digest,
        "background_group_comparison": isinstance(header, dict) and header.get("background_group_comparison") is True,
    }


def catalog_router():
    from .router import _trusted_agent_id

    router = APIRouter()

    @router.get("/agents/{agent_id}/skills")
    @router.get("/agents/{agent_id}/skills/{skill_id}")
    async def read(
        request: Request,
        agent_id: str,
        skill_id: str | None = None,
        authorization: str | None = Header(default=None),
        header_agent_id: str | None = Header(default=None, alias="X-Agent-Id"),
    ):
        trusted = _trusted_agent_id(authorization, header_agent_id, agent_id)
        agent = await get_agent_for_request(request, trusted)
        workspace = Path(agent.workspace_dir)
        if skill_id:
            return await run_in_threadpool(native_document, workspace, skill_id)
        return {"items": await run_in_threadpool(native_catalog, workspace)}

    return router
