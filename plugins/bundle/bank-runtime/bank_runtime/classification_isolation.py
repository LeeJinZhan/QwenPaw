"""Fail-closed
execution boundary for the dedicated classification workspace.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from qwenpaw.runtime.hooks import HookBase, HookResult
from qwenpaw.runtime.phases import Phase
from qwenpaw.app.crons.execution_context import get_cron_execution_identity

AGENT_ID = "classification-operations"
SKILL_NAME = "bank-classification-operations"
TOOL_NAMES = frozenset(
    {
        "classification_operations__claim_tasks",
        "classification_operations__submit_classification",
    }
)
POLICY_VERSION = "classification-isolation-1"
STARTING_HOOK = "bank-runtime:classification_isolation_before_services"


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()


def atomic_record(path: Path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(value, sort_keys=True, ensure_ascii=False), encoding="utf-8"
    )
    os.replace(temporary, path)


def check_publication(old, payload, actual_sha=None):
    """Fence delayed requests before touching native artifacts."""
    epoch = old.get("publication_epoch") or 0
    incoming = payload.publication_epoch or 0
    if epoch and not incoming:
        raise ValueError("Publication epoch required")
    if bool(payload.operation_id) != bool(payload.publication_epoch):
        raise ValueError("Publication identity incomplete")
    if incoming < epoch:
        raise ValueError("Publication epoch stale")
    same = old.get("operation_id") == payload.operation_id and bool(
        payload.operation_id
    )
    if incoming == epoch and incoming and not same:
        raise ValueError("Publication epoch conflict")
    if same:
        if (
            old.get("target_mapping_version", old.get("mapping_version"))
            != payload.mapping_version
            or old.get("target_skill_sha256", old.get("skill_sha256"))
            != payload.skill_sha256
        ):
            raise ValueError("Publication content changed")
        if old.get("publication_status") == "synced":
            return True
    expected = payload.expected_skill_sha256
    if expected is not None and expected != (
        actual_sha
        if actual_sha is not None
        else (old.get("skill_sha256") or "")
    ):
        raise ValueError("Previous Skill changed")
    return False


def parameter_observation(profile):
    """Accept only a provider's explicit versioned effective-parameter proof.

    The public optional adapter is read-only. A profile/config hash cannot
    prove
    gateway defaults; unsupported providers remain unknown and cannot pass
    eval.
    """
    unknown = {
        "effective_parameters_sha256": None,
        "parameters_state": "unknown",
    }
    try:
        from qwenpaw.providers import ProviderManager

        provider = ProviderManager.get_instance().get_provider(
            profile.active_model.provider_id
        )
        adapter = getattr(
            provider, "get_classification_parameter_evidence", None
        )
        if not callable(adapter):
            return unknown
        evidence = adapter(profile.active_model.model)
        if not isinstance(evidence, dict) or set(evidence) != {
            "source_version",
            "model_revision",
            "gateway_revision",
            "parameters",
        }:
            return unknown
        if any(
            not isinstance(evidence[key], str)
            or not evidence[key]
            or len(evidence[key]) > 128
            for key in ("source_version", "model_revision", "gateway_revision")
        ):
            return unknown
        parameters = evidence["parameters"]
        if not isinstance(parameters, dict) or set(parameters) != {
            "temperature",
            "top_p",
            "max_tokens",
            "seed",
        }:
            return unknown
        if any(
            value is not None
            and (
                isinstance(value, bool) or not isinstance(value, (int, float))
            )
            for value in parameters.values()
        ):
            return unknown
        import math

        if any(
            isinstance(value, float) and not math.isfinite(value)
            for value in parameters.values()
        ):
            return unknown
        return {
            "effective_parameters_sha256": digest(evidence),
            "parameters_state": "verified",
        }
    except (AttributeError, ValueError, KeyError, RuntimeError):
        return unknown


def selected_request_policy(
    execution_mode="scheduled",
    *,
    provider_verified=False,
    requested_policy=None,
    runtime_environment=None,
):
    local = (
        os.environ.get("QWENPAW_CLASSIFICATION_ENVIRONMENT")
        in {"development", "test"}
        and os.environ.get("QWENPAW_CLASSIFICATION_REQUEST_POLICY")
        == "local_wire_v1"
        and execution_mode == "scheduled"
        and not provider_verified
        and (
            requested_policy is None
            or requested_policy == "local_wire_v1"
            and runtime_environment
            == os.environ.get("QWENPAW_CLASSIFICATION_ENVIRONMENT")
        )
    )
    return "local_wire_v1" if local else "provider_verified"


def request_parameter_observation(profile):
    unknown = {
        "request_parameters_state": "unknown",
        "request_parameters_sha256": None,
    }
    try:
        from qwenpaw.providers import ProviderManager

        provider = ProviderManager.get_instance().get_provider(
            profile.active_model.provider_id
        )
        if provider is None or provider.id != profile.active_model.provider_id:
            return unknown
        model = provider.get_chat_model_instance(profile.active_model.model)
        observe = getattr(
            model, "get_classification_request_observation", None
        )
        value = observe() if callable(observe) else None
        if (
            not isinstance(value, dict)
            or value.get("provider_id") != profile.active_model.provider_id
            or value.get("model_id") != profile.active_model.model
            or value.get("schema_version") != "qwenpaw-client-request-1"
        ):
            return unknown
        return {
            "request_parameters_state": "verified",
            "request_parameters_sha256": digest(value),
        }
    except Exception:
        return unknown


def verify_hook_dependencies(workspace):
    registry = workspace.plugins.hook_registry
    required = {
        Phase.PRE_AGENT_BUILD: ("session_load", "classification_build_guard"),
        Phase.PRE_EXECUTE: (
            "cron_memory_isolate",
            "bootstrap",
            "skill_env_override",
            "classification_execution_guard",
        ),
        Phase.POST_RESPONSE: ("cron_memory_restore", "session_save"),
        Phase.PRE_DISPATCH: ("cron_context",),
    }
    for phase, names in required.items():
        hooks = [hook.name for hook in registry.hooks_for(phase)]
        if any(name not in hooks for name in names):
            raise ValueError("Classification isolation hook missing")
        if phase in (Phase.PRE_AGENT_BUILD, Phase.PRE_EXECUTE):
            if hooks[-1] != names[-1]:
                raise ValueError("Classification isolation hook must run last")
        if phase == Phase.POST_RESPONSE and hooks.index(
            "cron_memory_restore"
        ) > hooks.index("session_save"):
            raise ValueError("Classification history restore order invalid")


async def stop_workspace_cron(workspace):
    if workspace.cron_manager is None:
        return
    job = await workspace.cron_manager.get_job(AGENT_ID)
    if job is not None and job.enabled:
        await workspace.cron_manager.create_or_replace_job(
            job.model_copy(update={"enabled": False})
        )


async def stop_cron(manager):
    from qwenpaw.config.utils import load_config

    if AGENT_ID not in load_config().agents.profiles:
        return
    await stop_workspace_cron(await manager.get_agent(AGENT_ID))


async def observe_workspace(workspace):
    from qwenpaw.agents.skill_system import resolve_effective_skills
    from qwenpaw.config.config import load_agent_config

    if workspace.agent_id != AGENT_ID:
        raise ValueError("Classification workspace identity mismatch")
    root = Path(workspace.workspace_dir)
    receipt = json.loads(
        (root / "classification-mapping.json").read_text(encoding="utf-8")
    )
    from qwenpaw.agents.skill_system.store import read_skill_manifest

    mapping = (
        read_skill_manifest(root)
        .get("skills", {})
        .get(SKILL_NAME, {})
        .get("config")
        or {}
    )
    job = (
        await workspace.cron_manager.get_job(AGENT_ID)
        if workspace.cron_manager
        else None
    )
    skills = resolve_effective_skills(root, "console")
    skill_sha = hashlib.sha256(
        (root / "skills" / SKILL_NAME / "SKILL.md")
        .read_text(encoding="utf-8")
        .encode()
    ).hexdigest()
    agents_sha = hashlib.sha256((root / "AGENTS.md").read_bytes()).hexdigest()
    profile = load_agent_config(AGENT_ID)
    from qwenpaw.app.mcp.config_service import MCPConfigService

    service = MCPConfigService(workspace)
    cards = await service.list_cards()
    tools = await service.list_tools("classification_operations")
    actual_tools = sorted(
        "classification_operations__" + tool.name
        for tool in tools
        if tool.enabled
    )
    policy = await service.get_policy("classification_operations")
    expected_policy = {
        "default_effect": "deny",
        "tool_overrides": [
            {
                "tool_name": name,
                "source_value": "console",
                "subject_type": "user",
                "subject_value": AGENT_ID,
                "effect": "allow",
            }
            for name in ("claim_tasks", "submit_classification")
        ],
    }
    from qwenpaw.app.mcp.schemas import MCPAccessPolicy

    parameters = parameter_observation(profile)
    request_parameters = request_parameter_observation(profile)
    request_policy = selected_request_policy(
        receipt.get("execution_mode", "scheduled"),
        provider_verified=parameters["parameters_state"] == "verified",
        requested_policy=receipt.get(
            "runtime_request_policy", "provider_verified"
        ),
        runtime_environment=receipt.get("runtime_environment"),
    )
    request_matches = receipt.get(
        "request_policy", "provider_verified"
    ) == request_policy and (
        receipt.get("request_parameters_state") != "verified"
        or request_parameters
        == {
            key: receipt.get(key)
            for key in (
                "request_parameters_state",
                "request_parameters_sha256",
            )
        }
    )
    parameters_match = receipt.get(
        "parameters_state"
    ) != "verified" or parameters == {
        key: receipt.get(key)
        for key in ("parameters_state", "effective_parameters_sha256")
    }
    evaluation_environment = (
        receipt.get("execution_mode") != "manual_evaluation"
        or os.environ.get("QWENPAW_CLASSIFICATION_ENVIRONMENT") == "test"
    )
    verified = (
        STARTING_HOOK in profile.required_starting_hooks
        and evaluation_environment
        and request_matches
        and parameters_match
        and receipt.get("publication_status") == "synced"
        and skills == [SKILL_NAME]
        and receipt.get("tenant_id") == mapping.get("runtime_tenant_id")
        and receipt.get("mapping_version")
        == mapping.get("runtime_mapping_version")
        and receipt.get("skill_sha256") == mapping.get("skill_sha256")
        and receipt.get("skill_sha256") == skill_sha
        and receipt.get("agents_sha256") == agents_sha
        and job is not None
        and receipt.get("job_sha256") == digest(job.model_dump(mode="json"))
        and actual_tools == sorted(TOOL_NAMES)
        and not any(
            card.enabled and card.name != "classification_operations"
            for card in cards
        )
        and policy.model_dump()
        == MCPAccessPolicy.model_validate(expected_policy).model_dump()
        and not any(
            tool.enabled for tool in profile.tools.builtin_tools.values()
        )
        and profile.running.memory_manager_backend == "none"
        and not profile.heartbeat.enabled
        and not profile.plan.enabled
        and not profile.coding_mode.enabled
        and receipt.get("model_id") == profile.active_model.model
        and receipt.get("provider_id") == profile.active_model.provider_id
    )
    verify_hook_dependencies(workspace)
    state = workspace.cron_manager.get_state(AGENT_ID)
    observed = {
        key: receipt.get(key)
        for key in (
            "tenant_id",
            "operation_id",
            "publication_epoch",
            "mapping_version",
            "execution_mode",
            "model_id",
            "provider_id",
            "sync_state",
        )
    }
    observed.update(
        agent_id=AGENT_ID,
        cron_job_id=AGENT_ID,
        skill_name=SKILL_NAME,
        skill_sha256=skill_sha,
        isolation_state="verified" if verified else "drifted",
        skills=skills,
        tools=actual_tools,
        policy_version=POLICY_VERSION,
        cron_enabled=job.enabled if job else None,
        sync_state=receipt.get("publication_status"),
        model_id=profile.active_model.model,
        provider_id=profile.active_model.provider_id,
        checked_at=datetime.now(timezone.utc).isoformat(),
        cron_state={
            key: getattr(state, key).isoformat()
            if getattr(state, key)
            else None
            for key in ("last_run_at", "next_run_at")
        },
        last_run_status=state.last_status,
    )
    observed.update(observed["cron_state"])
    observed.update(parameters)
    observed.update(request_parameters, request_policy=request_policy)
    return observed


async def reject(workspace):
    try:
        await stop_workspace_cron(workspace)
    finally:
        raise ValueError("CLASSIFICATION_ISOLATION_REJECTED") from None


def normalized_input(value):
    if isinstance(value, dict):
        return {
            key: normalized_input(item)
            for key, item in value.items()
            if key not in {"id", "msg_id"}
        }
    if isinstance(value, list):
        return [normalized_input(item) for item in value]
    return value


class ClassificationBuildGuard(HookBase):
    phase = Phase.PRE_AGENT_BUILD
    name = "classification_build_guard"
    priority = 10000
    after = ("session_load", "coding_mode_project_dir", "mission_state_load")

    async def run(self, ctx):
        workspace = ctx.workspace
        if getattr(workspace, "agent_id", None) != AGENT_ID:
            if ctx.agent_id == AGENT_ID:
                raise ValueError("CLASSIFICATION_IDENTITY_REJECTED")
            return HookResult()
        try:
            if ctx.agent_id != AGENT_ID or str(
                Path(ctx.workspace_dir).resolve()
            ) != str(Path(workspace.workspace_dir).resolve()):
                raise ValueError("Identity mismatch")
            provenance = get_cron_execution_identity()
            if (
                provenance is None
                or provenance.workspace is not workspace
                or provenance.job_id != AGENT_ID
            ):
                raise ValueError("Trusted Cron provenance required")
            observed = await observe_workspace(workspace)
            if observed["isolation_state"] != "verified":
                raise ValueError("Classification drift")
            job = await workspace.cron_manager.get_job(AGENT_ID)
            request = ctx.request
            context = request.request_context
            from qwenpaw.schemas import AgentRequest

            expected_input = normalized_input(
                AgentRequest(input=job.request.input).model_dump(mode="json")[
                    "input"
                ]
            )
            actual_input = normalized_input(
                AgentRequest(input=request.input).model_dump(mode="json")[
                    "input"
                ]
            )
            if (
                not ctx.extras.get("is_cron")
                or request.session_source != "cron"
                or context.get("cron_job_id") != AGENT_ID
                or request.user_id != AGENT_ID
                or request.channel != "console"
                or actual_input != expected_input
                or getattr(request, "model_slot_override", None) is not None
            ):
                raise ValueError("Managed Cron request required")
            evaluation = observed.get("execution_mode") == "manual_evaluation"
            if (
                evaluation
                and os.environ.get("QWENPAW_CLASSIFICATION_ENVIRONMENT")
                != "test"
            ):
                raise ValueError("Evaluation isolation required")
            if not job.enabled and not evaluation:
                raise ValueError("Classification disabled")
            context["subagent_skills"] = [SKILL_NAME]
            context["subagent_allowed_tools"] = sorted(TOOL_NAMES)
            ctx.extras["classification_isolation_verified"] = True
        except Exception:
            await reject(workspace)
        return HookResult()


from agentscope.tool import Toolkit  # noqa: E402


class ClassificationToolkit(Toolkit):
    """Restrict even dependency-provided Skill/meta tools to the two MCP calls.

    The fixed Skill is already included in the managed system prompt. The
    generic Skill viewer is unnecessary here and must not expand capabilities.
    """

    @classmethod
    def from_toolkit(cls, toolkit):
        basic = next(
            group for group in toolkit.tool_groups if group.name == "basic"
        )
        return cls(
            tools=basic.tools,
            skills_or_loaders=basic.skills_or_loaders,
            mcps=basic.mcps,
            tool_groups=[
                group for group in toolkit.tool_groups if group.name != "basic"
            ],
        )

    async def get_tool_schemas(self, groups=None):
        schemas = await super().get_tool_schemas(groups)
        return [
            schema
            for schema in schemas
            if schema["function"]["name"] in TOOL_NAMES
        ]

    async def check_tool_available(self, tool_name, activated_groups):
        if tool_name not in TOOL_NAMES:
            raise ValueError("CLASSIFICATION_TOOL_REJECTED")
        return await super().check_tool_available(tool_name, activated_groups)

    async def get_tool(self, tool_name):
        if tool_name not in TOOL_NAMES:
            raise ValueError("CLASSIFICATION_TOOL_REJECTED")
        return await super().get_tool(tool_name)

    async def call_tool(self, tool_call, state):
        if tool_call.name not in TOOL_NAMES:
            raise ValueError("CLASSIFICATION_TOOL_REJECTED")
        async for chunk in super().call_tool(tool_call, state):
            yield chunk


class ClassificationExecutionGuard(HookBase):
    phase = Phase.PRE_EXECUTE
    name = "classification_execution_guard"
    priority = 10000
    after = (
        "cron_memory_isolate",
        "bootstrap",
        "skill_env_override",
        "media_process",
    )

    async def run(self, ctx):
        if getattr(ctx.workspace, "agent_id", None) != AGENT_ID:
            return HookResult()
        try:
            if not ctx.extras.get("classification_isolation_verified"):
                raise ValueError("Build guard missing")
            if ctx.agent.state.context or ctx.agent.state.summary:
                raise ValueError("Cron context not isolated")
            if (
                "_cron_context_snapshot" not in ctx.extras
                or ctx.context_injections
            ):
                raise ValueError("History isolation or prompt drift")
            toolkit = ctx.agent.toolkit
            loaded = [
                skill
                for group in toolkit.tool_groups
                for skill in await group.list_skills()
            ]
            skills = [skill.name for skill in loaded]
            import frontmatter

            content = (
                Path(ctx.workspace.workspace_dir)
                / "skills"
                / SKILL_NAME
                / "SKILL.md"
            ).read_text(encoding="utf-8")
            expected_skill = frontmatter.loads(content)
            if len(loaded) != 1 or (
                Path(loaded[0].dir).resolve()
                != (
                    Path(ctx.workspace.workspace_dir) / "skills" / SKILL_NAME
                ).resolve()
                or loaded[0].markdown != expected_skill.content
                or loaded[0].description != expected_skill["description"]
            ):
                raise ValueError("Loaded Skill content mismatch")
            declared = [
                tool.name
                for group in toolkit.tool_groups
                for tool in group.tools
            ]
            if (
                len(declared) != 2
                or set(declared) != TOOL_NAMES
                or any(group.mcps for group in toolkit.tool_groups)
            ):
                raise ValueError("Unexpected tool group capabilities")
            schemas = await toolkit.get_tool_schemas(
                groups=[group.name for group in toolkit.tool_groups]
            )
            tools = [schema["function"]["name"] for schema in schemas]
            # AgentScope adds its Skill viewer outside the workspace whitelist.
            # Reject all other additions before installing the local adapter.
            if (
                skills != [SKILL_NAME]
                or set(tools) - {"Skill"} != TOOL_NAMES
                or len(tools) != len(set(tools))
            ):
                raise ValueError("Final capabilities mismatch")
            if not isinstance(toolkit, ClassificationToolkit):
                ctx.agent.toolkit = ClassificationToolkit.from_toolkit(toolkit)
            final = await ctx.agent.toolkit.get_tool_schemas(
                groups=[group.name for group in toolkit.tool_groups]
            )
            if {schema["function"]["name"] for schema in final} != TOOL_NAMES:
                raise ValueError("Final capabilities mismatch")
            observed = await observe_workspace(ctx.workspace)
            if observed["isolation_state"] != "verified":
                raise ValueError("Final content drift")
            identity = getattr(
                ctx.agent.model,
                "get_classification_runtime_model_identity",
                None,
            )
            expected = {
                key: observed.get(key) for key in ("provider_id", "model_id")
            }
            if (
                not callable(identity)
                or any(not value for value in expected.values())
                or identity() != expected
            ):
                raise ValueError(
                    "Actual classification model identity mismatch"
                )
            if observed.get("request_parameters_state") == "verified":
                request = getattr(
                    ctx.agent.model,
                    "get_classification_request_observation",
                    None,
                )
                bind = getattr(
                    ctx.agent.model, "bind_classification_request_guard", None
                )
                expected_sha = observed.get("request_parameters_sha256")
                expected_policy = observed.get("request_policy")
                receipt = json.loads(
                    (
                        Path(ctx.workspace.workspace_dir)
                        / "classification-mapping.json"
                    ).read_text(encoding="utf-8")
                )
                if (
                    not callable(request)
                    or not callable(bind)
                    or digest(request()) != expected_sha
                ):
                    raise ValueError(
                        "Actual client request parameters mismatch"
                    )

                async def wire_guard(value):
                    if (
                        selected_request_policy(
                            observed.get("execution_mode", "scheduled"),
                            provider_verified=observed.get("parameters_state")
                            == "verified",
                            requested_policy=receipt.get(
                                "runtime_request_policy", "provider_verified"
                            ),
                            runtime_environment=receipt.get(
                                "runtime_environment"
                            ),
                        )
                        != expected_policy
                        or not isinstance(value, dict)
                        or digest(value) != expected_sha
                    ):
                        await reject(ctx.workspace)

                bind(wire_guard)
            elif observed.get("request_policy") == "local_wire_v1":
                raise ValueError("Local client request parameters unknown")
        except Exception:
            await reject(ctx.workspace)
        return HookResult()


def classification_workspace_starting(workspace_info):
    """Install classification guards before persisted native Cron can fire."""
    workspace = workspace_info.get("workspace")
    if getattr(workspace, "agent_id", None) != AGENT_ID:
        return
    if workspace_info.get("agent_id") != AGENT_ID:
        raise ValueError("CLASSIFICATION_STARTUP_IDENTITY_MISMATCH")
    workspace.plugins.hook_registry.register(ClassificationBuildGuard())
    workspace.plugins.hook_registry.register(ClassificationExecutionGuard())
    verify_hook_dependencies(workspace)


def restore_starting_hook_requirement():
    """Upgrade only native safety metadata on an already-published workspace.

    Intended for an explicitly requested local restart preflight. It never
    starts a workspace or changes jobs, Skill contents or publication proof.
    """
    from qwenpaw.config.utils import load_config
    from qwenpaw.config.config import load_agent_config, save_agent_config
    from qwenpaw.agents.skill_system.store import read_skill_manifest
    from qwenpaw.app.crons.models import CronJobSpec

    reference = load_config().agents.profiles.get(AGENT_ID)
    if reference is None:
        return {"state": "unconfigured", "changed": False}
    root = Path(reference.workspace_dir)
    files = [
        root / "classification-mapping.json",
        root / "AGENTS.md",
        root / "jobs.json",
        root / "skills" / SKILL_NAME / "SKILL.md",
        root / "agent.json",
        root / "skill.json",
    ]
    if any(not path.is_file() or path.is_symlink() for path in files):
        raise ValueError("CLASSIFICATION_RESTORE_METADATA_UNAVAILABLE")
    receipt = json.loads(files[0].read_text(encoding="utf-8"))
    manifest = read_skill_manifest(root).get("skills", {}).get(SKILL_NAME, {})
    mapping = manifest.get("config") or {}
    profile = load_agent_config(AGENT_ID)
    jobs = json.loads((root / "jobs.json").read_text(encoding="utf-8")).get(
        "jobs", []
    )
    fixed = [
        CronJobSpec.model_validate(job)
        for job in jobs
        if job.get("id") == AGENT_ID
    ]
    if (
        profile.id != AGENT_ID
        or receipt.get("agent_id") != AGENT_ID
        or receipt.get("publication_status") != "synced"
        or manifest.get("enabled") is not True
        or manifest.get("channels") != ["console"]
        or receipt.get("tenant_id") != mapping.get("runtime_tenant_id")
        or receipt.get("mapping_version")
        != mapping.get("runtime_mapping_version")
        or not isinstance(receipt.get("tenant_id"), str)
        or not receipt["tenant_id"]
        or not isinstance(receipt.get("mapping_version"), str)
        or not receipt["mapping_version"]
        or receipt.get("skill_sha256") != mapping.get("skill_sha256")
        or receipt.get("skill_sha256")
        != hashlib.sha256(
            files[3].read_text(encoding="utf-8").encode()
        ).hexdigest()
        or receipt.get("agents_sha256")
        != hashlib.sha256(files[1].read_bytes()).hexdigest()
        or len(fixed) != 1
        or len(jobs) != 1
        or receipt.get("job_sha256")
        != digest(fixed[0].model_dump(mode="json"))
        or not profile.active_model
        or receipt.get("model_id") != profile.active_model.model
        or receipt.get("provider_id") != profile.active_model.provider_id
        or any(tool.enabled for tool in profile.tools.builtin_tools.values())
        or profile.running.memory_manager_backend != "none"
        or profile.heartbeat.enabled
        or profile.plan.enabled
        or profile.coding_mode.enabled
    ):
        raise ValueError("CLASSIFICATION_RESTORE_METADATA_DRIFT")
    required = profile.required_starting_hooks
    if STARTING_HOOK in required:
        return {"state": "verified", "changed": False}
    profile.required_starting_hooks = [*required, STARTING_HOOK]
    save_agent_config(AGENT_ID, profile)
    if (
        load_agent_config(AGENT_ID).required_starting_hooks
        != profile.required_starting_hooks
    ):
        raise ValueError("CLASSIFICATION_RESTORE_READBACK_FAILED")
    return {"state": "verified", "changed": True}
