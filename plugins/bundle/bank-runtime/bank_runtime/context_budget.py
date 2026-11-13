"""Read-only prompt/schema compilation and explicit bank model budgets.

No Agent, Session, Governor, offloader or business-tool invocation is created.
UTF-8 bytes are a conservative bound, not a reported model tokenizer count.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from agentscope.state import AgentState
from agentscope.tool import FunctionTool, Toolkit
from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from .auth import require_service_identity
from .content_review import require_review_agent
from .coordination import coordination_from_request, render_coordination_prompt
from .production_guard import require_production_readiness
from .remote_state import canonical_hash, canonical_json


class BudgetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request: dict[str, Any]


def configured_budget(info: Any, defaults: dict[str, Any]) -> tuple[int, int]:
    from qwenpaw.providers.context_windows import DEFAULT_CONTEXT_WINDOW
    limit = getattr(info, "max_input_length", None)
    explicit = getattr(info, "max_input_length_configured", False)
    if type(limit) is not int or limit < 1000 or not (explicit is True or limit != DEFAULT_CONTEXT_WINDOW):
        raise ValueError("Model context window is not configured")
    output = getattr(info, "max_tokens", None)
    for name in ("max_tokens", "max_completion_tokens", "max_output_tokens"):
        if name in defaults:
            output = defaults[name]
    if type(output) is not int or not 1 <= output < limit:
        raise ValueError("Model output reserve is not configured")
    return limit, output


def bound(value: Any) -> int:
    return len((value if isinstance(value, str) else canonical_json(value)).encode("utf-8"))


def policy_digest(material: dict[str, Any]) -> str:
    return canonical_hash(material)


def skill_directory_digest(directory: Path) -> str:
    """Hash the complete published skill tree without following links."""
    records, total = [], 0
    for parent, directories, filenames in os.walk(directory, followlinks=False):
        directories.sort()
        if any((Path(parent) / name).is_symlink() for name in directories):
            raise ValueError('Symlinked skill directory')
        for filename in sorted(filenames):
            path = Path(parent) / filename
            if path.is_symlink() or not path.is_file():
                raise ValueError('Invalid skill resource')
            size = path.stat().st_size
            total += size
            if len(records) >= 2048 or total > 64 * 1024 * 1024:
                raise ValueError('Skill directory exceeds digest limit')
            digest = hashlib.sha256()
            with path.open('rb') as stream:
                for block in iter(lambda: stream.read(65536), b''):
                    digest.update(block)
            records.append({'path': path.relative_to(directory).as_posix(), 'hash': digest.hexdigest()})
    return canonical_hash(records)


def attachment_input_bound(manifest: list[dict[str, Any]], native: bool) -> int:
    """Bound text insertion without downloading or authorizing attachments."""
    total = 0
    remaining = 384000
    media_extensions = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff",
        ".mp3", ".wav", ".m4a", ".mp4", ".webm", ".mov"}
    for item in manifest:
        extension = Path(str(item.get("display_name", ""))).suffix.lower()
        mime = str(item.get("content_type", ""))
        if extension in media_extensions or mime.startswith(("image/", "audio/", "video/")):
            raise ValueError("Unknown media token budget")
        total += bound(item) + 2048  # fixed file status/reference/container metadata envelope
        if not native and extension not in {".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".pdf", ".csv", ".tsv"}:
            size = item.get("size_bytes")
            chars = min(128000, remaining, size if type(size) is int and size >= 0 else 128000)
            total += 4 * chars
            remaining -= chars
    return total


async def compile_budget(agent_id: str, payload: dict[str, Any], workspace: Any,
                         *, configuration_sink: dict | None = None) -> dict[str, Any]:
    captured = {}
    try:
        result = await _compile_budget(agent_id, payload, workspace, captured)
        if configuration_sink is not None:
            configuration_sink.update(captured)
        return result
    finally:
        if captured and (configuration_sink is None or not configuration_sink):
            captured['configuration'].close()


async def _compile_budget(agent_id, payload, workspace, captured):
    from qwenpaw.config.config import load_agent_config
    from qwenpaw.providers import ProviderManager
    from qwenpaw.runtime.builder import AgentBuilder
    from qwenpaw.agents.skill_system.registry import resolve_effective_skills
    from .personalization import _compile_profile, _security_boundary, preferred_method_prompt
    from .personal_skills import build_catalog_prompt, activate_personal_skill
    from .presentation import PUBLIC_RESPONSE_GUIDANCE
    from .gateway.visibility import parse_runtime_tool_visibility
    from .gateway.delegations import RuntimeDelegationTools
    from .gateway.result_recall import RuntimeResultReader
    from .sandbox import native_tools
    from .sandbox.hooks import _sandbox_guidance, _analysis_environment_guidance
    from .sandbox.tools import runtime_sandbox_files_search, runtime_sandbox_files_select
    from .bank_assistant import bank_assistant
    from .artifact_tools import artifact_generate, artifact_revise, artifact_convert, template_fill_docx

    if len(canonical_json(payload).encode("utf-8")) > 4 * 1024 * 1024:
        raise ValueError("Budget input exceeds limit")
    loaded_release = None
    release_id = payload.get('runtime_config_release')
    if release_id is not None:
        if not payload.get('runtime_worker_node_id') or payload['runtime_worker_node_id'] != os.environ.get('QWENPAW_NODE_ID'):
            raise ValueError('Configuration worker node mismatch')
        from .config_release import configured_cache
        loaded_release = configured_cache().load(agent_id, release_id)
        config, provider = loaded_release.config, loaded_release.provider
        slot = config.active_model
    else:
        config = copy.deepcopy(load_agent_config(agent_id))
        manager = ProviderManager.get_instance()
        slot = config.active_model or manager.get_active_model()
        provider = copy.deepcopy(manager.get_provider(slot.provider_id)) if slot else None
    if payload.get("model_slot_override") is not None or not slot or not slot.provider_id or not slot.model:
        raise ValueError("Unknown effective model")
    if provider is None:
        raise ValueError("Unknown provider")
    info = provider.get_model_info(slot.model)
    defaults = provider.get_effective_generate_kwargs(slot.model)
    limit, output = configured_budget(info, defaults)
    projection = parse_runtime_tool_visibility(payload.get("runtime_tool_visibility"))
    if projection is None or projection.worker_type != "qwenpaw":
        raise ValueError("Missing authoritative tool schema scope")
    allowed = set(projection.worker_tool_names)
    if loaded_release is not None and not allowed.issubset(loaded_release.manifest['policy']['tool_names']):
        raise ValueError('Tool projection exceeds published configuration')
    request = SimpleNamespace(**{**payload, "channel": "bank-runtime"})
    coordination = coordination_from_request(request)
    ctx = SimpleNamespace(agent_id=agent_id, session_id=str(payload.get("session_id", "")),
        request=request, workspace=workspace, workspace_dir=workspace.workspace_dir, extras={})
    builder = AgentBuilder()
    rc = builder._build_request_context(ctx)
    skills = ([Path(path).name for path in loaded_release.skill_directories] if loaded_release is not None
              else list(resolve_effective_skills(workspace.workspace_dir, "bank-runtime")))
    role = coordination.get("role_snapshot")
    if role:
        skills = [name for name in role.get("skill_ids", []) if name in skills]
    modes = workspace.plugins.active_mode_names(ctx)
    from .execution_configuration import ExecutionConfiguration
    config.active_model = copy.deepcopy(slot)
    configuration = ExecutionConfiguration(config, provider, skills, modes)
    captured['configuration'] = configuration
    ctx.extras['request_configuration'] = configuration
    if loaded_release is not None:
        configuration.skill_directories([path for path in loaded_release.skill_directories if Path(path).name in skills])
    drivers, hints = await builder._collect_driver_tools_and_prompts(ctx, rc)
    ctx.extras["driver_prompt_hints"] = hints
    toolkit = await builder.build_toolkit(config, agent_id=agent_id, request_context=rc,
        active_modes=modes, effective_skills=skills, extra_tools=drivers, ctx=ctx,
        workspace_dir=workspace.workspace_dir)
    additional = [bank_assistant, artifact_generate, artifact_revise, artifact_convert, template_fill_docx,
        runtime_sandbox_files_search, runtime_sandbox_files_select]
    native = isinstance(payload.get("sandbox_context"), dict) and payload["sandbox_context"].get("native_analysis_enabled") is True
    if native:
        additional.extend(getattr(native_tools, name) for name in sorted(native_tools.NATIVE_TOOL_NAMES))
    if payload.get("personal_skills_catalog"):
        additional.append(activate_personal_skill)
    by_name = {tool.name: tool for group in toolkit.tool_groups for tool in group.tools}
    by_name.update({fn.__name__: FunctionTool(fn) for fn in additional})
    if allowed - set(by_name):
        raise ValueError("Published tool schema unavailable")
    for group in toolkit.tool_groups:
        group.tools[:] = []
    toolkit.tool_groups[0].tools.extend(by_name[name] for name in sorted(allowed))
    # Schema-only controlled readers. No managed scope means no transport client,
    # no callable authority and no grant; actual execution installs exact tools.
    facade = SimpleNamespace(toolkit=toolkit, state=AgentState())
    result_reader = RuntimeResultReader(facade, None)
    toolkit.tool_groups[0].tools.append(result_reader.tool)
    if not role:
        toolkit.tool_groups[0].tools.extend(RuntimeDelegationTools(facade, None).tools)
    if payload.get("personal_skills_catalog") and "activate_personal_skill" not in allowed:
        toolkit.tool_groups[0].tools.append(FunctionTool(activate_personal_skill))
    schemas = await toolkit.get_tool_schemas()
    skill_prompt = await toolkit.get_skill_instructions() or ""
    skill_versions = configuration.skill_versions()
    profile = _compile_profile(payload.get("runtime_context"))
    personal_catalog = build_catalog_prompt(payload.get("personal_skills_catalog")) if payload.get("personal_skills_catalog") else ""
    method = preferred_method_prompt(payload.get("preferred_method"))
    coordinated = render_coordination_prompt(coordination)
    manifest = payload.get("attachments_manifest") or []
    if not isinstance(manifest, list) or len(manifest) > 50 or any(not isinstance(item, dict) for item in manifest):
        raise ValueError("Invalid attachment manifest")
    sandbox = payload.get("sandbox_context") or {}
    stable_guidance = "\n\n".join((_security_boundary(), PUBLIC_RESPONSE_GUIDANCE,
        _sandbox_guidance(0, allowed, native_analysis_enabled=native), coordinated))
    base = builder.build_prompt(ctx, config)
    if loaded_release is not None:
        base += '\n\n' + loaded_release.manifest['policy']['prompt']
    configuration.base_prompt = base
    # Env data changes by session/time but is not policy. Hash the exact remaining
    # system instructions; account for the actual environment bytes in the budget.
    environment = builder._build_env_context(ctx, config)
    stable_base = base.replace(environment, "<request-environment>")
    system = "\n\n".join((base, stable_guidance, profile, method, personal_catalog,
        _sandbox_guidance(len(manifest), allowed, native_analysis_enabled=native),
        _analysis_environment_guidance(sandbox)))
    current = payload.get("input", payload.get("input_text", ""))
    # Envelope/framing and bounded per-model middleware instructions are reserved
    # here; the final actual message/schema envelope is checked again before LLM I/O.
    input_tokens = bound(current) + attachment_input_bound(manifest, native) + bound(payload.get("runtime_context") or {})
    from .release import BANK_RUNTIME_PLUGIN_VERSION
    material = {"model": {"provider_id": slot.provider_id, "model": slot.model}, "context_limit": limit,
        "output_reserve_tokens": output, "system": stable_base + stable_guidance + profile + method + personal_catalog,
        "skills": skill_versions, "skill_prompt": configuration.normalize_paths(skill_prompt).replace(str(workspace.workspace_dir), '<workspace>'), "tools": schemas,
        # Node-local storage and channel delivery metadata are not execution
        # policy; identical releases must hash equally on different hosts.
        "agent_configuration": config.model_dump(mode='json', exclude={
            'workspace_dir', 'last_dispatch', 'channels', 'heartbeat',
            'name', 'description', 'template_id'}),
        "generate_kwargs": defaults,
        "model_info": info.model_dump(mode='json') if hasattr(info, 'model_dump') else vars(info),
        "provider_policy": {'implementation': type(provider).__module__ + '.' + type(provider).__qualname__,
            'base_url': getattr(provider, 'base_url', ''),
            'max_inline_media_bytes': getattr(provider, 'max_inline_media_bytes', None)},
        "plugin_version": BANK_RUNTIME_PLUGIN_VERSION, "build_digest": os.environ.get('QWENPAW_BUILD_DIGEST', ''),
        "visibility": sorted(allowed), "approval_level": config.approval_level}
    if loaded_release is not None:
        material['config_release_id'] = loaded_release.release_id
    return {"context_limit": limit, "context_limit_source": "configured_model",
        "model_key": canonical_hash({'provider':slot.provider_id, 'model':slot.model,
            'endpoint':str(getattr(provider,'base_url','')).rstrip('/')}),
        "token_count_source": "conservative_utf8_bytes", "system_tokens": bound(system) + 4096,
        "skills_tokens": bound(skill_prompt), "tool_schema_tokens": bound(schemas) + 1024,
        "input_tokens": input_tokens, "output_reserve_tokens": output,
        "max_summary_tokens": min(2048, max(256, limit // 16)), "policy_hash": policy_digest(material),
        **({'config_release_id': loaded_release.release_id, 'worker_node_id': os.environ['QWENPAW_NODE_ID']}
           if loaded_release is not None else {})}


async def verify_execution_budget(ctx: Any, execution: Any) -> str:
    from .production_guard import production_guard_enabled
    from .remote_state import RemoteStateError
    evidence = execution.context_package.get('budget_evidence')
    if evidence is None:
        if production_guard_enabled():
            raise RemoteStateError('WORKER_CONTEXT_BUDGET_UNAVAILABLE')
        return ''
    if not isinstance(evidence, dict):
        raise RemoteStateError('WORKER_CONTEXT_BUDGET_UNAVAILABLE')
    payload = ctx.request.model_dump(mode='json') if hasattr(ctx.request, 'model_dump') else vars(ctx.request).copy()
    payload.pop('runtime_execution', None)
    release_id = evidence.get('config_release_id')
    if payload.get('runtime_config_release') is not None and payload['runtime_config_release'] != release_id:
        raise RemoteStateError('WORKER_POLICY_MISMATCH')
    if release_id is not None:
        payload['runtime_config_release'] = release_id
    captured = {}
    try:
        actual = await compile_budget(ctx.agent_id, payload, ctx.workspace, configuration_sink=captured)
        if (actual['policy_hash'] != evidence.get('model_policy_hash')
                or actual['context_limit'] != evidence.get('context_limit')
                or actual['output_reserve_tokens'] != evidence.get('output_reserve_tokens')
                or getattr(execution,'model_capacity_enabled',False) and actual['model_key'] != evidence.get('model_key')):
            raise RemoteStateError('WORKER_POLICY_MISMATCH')
        if 'configuration' in captured:
            ctx.extras['request_configuration'] = captured.pop('configuration')
        return actual['policy_hash']
    finally:
        if captured:
            captured['configuration'].close()


def enforce_model_budget(input_kwargs: dict[str, Any]) -> None:
    """Fence final middleware-produced context before any model transport."""
    from .session import current_managed_session_scope
    from .remote_state import RemoteStateError
    scope = current_managed_session_scope()
    if scope is None or scope.remote_client is None:
        return
    evidence = scope.remote_client.execution.context_package.get("budget_evidence")
    if evidence is None:
        return  # compatibility with explicit, unprojected remote execution packages
    limit, reserve = evidence.get("context_limit"), evidence.get("output_reserve_tokens")
    if (type(limit) is not int or type(reserve) is not int or not 0 < reserve < limit
            or evidence.get("token_count_source") != "conservative_utf8_bytes"):
        raise RemoteStateError("WORKER_CONTEXT_BUDGET_UNAVAILABLE")
    messages = [msg.to_dict() if hasattr(msg, "to_dict") else msg for msg in input_kwargs.get("messages") or []]
    material = {"messages": messages, "tools": input_kwargs.get("tools") or []}
    if bound(material) + reserve + 256 > limit:
        raise RemoteStateError("WORKER_CONTEXT_BUDGET_EXCEEDED")


def build_context_budget_router() -> APIRouter:
    router = APIRouter()

    @router.post("/agents/{agent_id}/context/budget")
    async def budget(agent_id: str, payload: BudgetRequest, request: Request,
            authorization: str | None = Header(default=None),
            header_agent_id: str | None = Header(default=None, alias="X-Agent-Id")):
        trusted = require_service_identity(authorization, header_agent_id)
        if trusted != agent_id:
            raise HTTPException(401, "Agent identity mismatch")
        require_production_readiness()
        try:
            require_review_agent(trusted)
            from qwenpaw.app.agent_context import get_agent_for_request
            workspace = await get_agent_for_request(request, agent_id=trusted)
            return await asyncio.wait_for(compile_budget(trusted, payload.request, workspace), timeout=20)
        except Exception:
            raise HTTPException(503, {"code": "WORKER_CONTEXT_BUDGET_UNAVAILABLE"}) from None

    return router
