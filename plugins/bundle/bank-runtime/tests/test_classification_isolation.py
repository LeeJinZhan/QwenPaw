from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bank_runtime import classification_isolation as isolation  # noqa: E402
from bank_runtime.classification_operations import (  # noqa: E402
    MappingRequest,
    build_job,
)
from qwenpaw.runtime.hooks import (  # noqa: E402
    HookRegistry,
    HookBase,
    HookResult,
)
from qwenpaw.runtime.phases import Phase  # noqa: E402


def payload(**overrides):
    content = (
        "---\nname: bank-classification-operations\n"
        "description: classify tasks\n---\nUse only classification tools.\n"
    )
    values = dict(
        mapping_version="v1",
        skill_content=content,
        skill_sha256=hashlib.sha256(content.encode()).hexdigest(),
        runtime_base_url="http://127.0.0.1:8000",
        runtime_tenant_id="tenant",
        operations_token="test-only",
        operation_id="op-1",
        publication_epoch=1,
        expected_skill_sha256="",
        enabled=True,
    )
    return MappingRequest(**(values | overrides))


def registry():
    result = HookRegistry()
    phases = {
        Phase.PRE_AGENT_BUILD: ["session_load"],
        Phase.PRE_EXECUTE: [
            "cron_memory_isolate",
            "bootstrap",
            "skill_env_override",
        ],
        Phase.POST_RESPONSE: ["cron_memory_restore", "session_save"],
        Phase.PRE_DISPATCH: ["cron_context"],
    }
    for phase, names in phases.items():
        for index, name in enumerate(names):
            hook = HookBase()
            hook.phase, hook.name, hook.priority = phase, name, index
            result.register(hook)
    result.register(isolation.ClassificationBuildGuard())
    result.register(isolation.ClassificationExecutionGuard())
    return result


def workspace(tmp_path):
    job = build_job(payload())
    cron = NS(
        get_job=AsyncMock(return_value=job),
        create_or_replace_job=AsyncMock(),
        get_state=lambda _: NS(
            last_run_at=None, next_run_at=None, last_status=None
        ),
    )
    return NS(
        agent_id=isolation.AGENT_ID,
        workspace_dir=str(tmp_path),
        cron_manager=cron,
        plugins=NS(hook_registry=registry()),
    )


def context(ws):
    job = ws.cron_manager.get_job.return_value
    request = NS(
        request_context={"cron_job_id": isolation.AGENT_ID},
        session_source="cron",
        user_id=isolation.AGENT_ID,
        channel="console",
        input=job.request.input,
        model_slot_override=None,
    )
    return NS(
        workspace=ws,
        workspace_dir=Path(ws.workspace_dir),
        agent_id=isolation.AGENT_ID,
        request=request,
        session_id="classification-cron",
        extras={"is_cron": True, "_cron_context_snapshot": []},
        context_injections=[],
    )


@pytest.mark.parametrize(
    "change",
    [
        dict(publication_epoch=None, operation_id=None),
        dict(publication_epoch=1, operation_id="other"),
        dict(publication_epoch=1, mapping_version="changed"),
        dict(publication_epoch=1, skill_sha256="a" * 64),
    ],
)
def test_publication_fencing_rejects_stale_or_changed_content(change):
    old = dict(
        publication_epoch=2,
        operation_id="op-1",
        mapping_version="v1",
        skill_sha256=payload().skill_sha256,
        publication_status="synced",
    )
    with pytest.raises(ValueError):
        isolation.check_publication(old, payload(**change))


def test_publication_completed_retry_and_recovery_preserve_target():
    old = dict(
        publication_epoch=1,
        operation_id="op-1",
        mapping_version="v1",
        skill_sha256=payload().skill_sha256,
        publication_status="synced",
    )
    assert isolation.check_publication(old, payload()) is True
    assert (
        isolation.check_publication(old, payload(publication_epoch=2)) is True
    )
    old["publication_status"] = "syncing"
    with pytest.raises(ValueError, match="Previous"):
        isolation.check_publication(
            old, payload(expected_skill_sha256="b" * 64)
        )


def test_native_hook_dependencies_and_final_order(tmp_path):
    ws = workspace(tmp_path)
    isolation.verify_hook_dependencies(ws)
    extra = HookBase()
    extra.phase, extra.name, extra.priority = (
        Phase.PRE_EXECUTE,
        "late_mutation",
        20000,
    )
    ws.plugins.hook_registry.register(extra)
    with pytest.raises(ValueError, match="last"):
        isolation.verify_hook_dependencies(ws)


@pytest.mark.asyncio
async def test_build_guard_enforces_trusted_cron_and_overwrites_whitelists(
    monkeypatch, tmp_path
):
    ws = workspace(tmp_path)
    ctx = context(ws)
    monkeypatch.setattr(
        isolation,
        "get_cron_execution_identity",
        lambda: NS(workspace=ws, job_id=isolation.AGENT_ID),
    )
    ctx.request.request_context.update(
        subagent_skills=["evil"], subagent_allowed_tools=["shell"]
    )
    monkeypatch.setattr(
        isolation,
        "observe_workspace",
        AsyncMock(return_value={"isolation_state": "verified"}),
    )
    await isolation.ClassificationBuildGuard().run(ctx)
    assert ctx.request.request_context["subagent_skills"] == [
        isolation.SKILL_NAME
    ]
    assert (
        set(ctx.request.request_context["subagent_allowed_tools"])
        == isolation.TOOL_NAMES
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "wrong_identity",
        "direct_dialogue",
        "wrong_job",
        "model_override",
        "drift",
        "missing_cron",
    ],
)
@pytest.mark.asyncio
async def test_build_rejection_happens_before_model(
    monkeypatch, tmp_path, mutation
):
    ws = workspace(tmp_path)
    ctx = context(ws)
    monkeypatch.setattr(
        isolation,
        "get_cron_execution_identity",
        lambda: NS(workspace=ws, job_id=isolation.AGENT_ID),
    )
    observed = {"isolation_state": "verified"}
    if mutation == "wrong_identity":
        ctx.agent_id = "default"
    if mutation == "direct_dialogue":
        ctx.request.session_source = "console"
    if mutation == "wrong_job":
        ctx.request.request_context["cron_job_id"] = "other"
    if mutation == "model_override":
        ctx.request.model_slot_override = {"model": "other"}
    if mutation == "drift":
        observed["isolation_state"] = "drifted"
    if mutation == "missing_cron":
        ctx.extras.clear()
    monkeypatch.setattr(
        isolation, "observe_workspace", AsyncMock(return_value=observed)
    )
    model = AsyncMock()
    with pytest.raises(ValueError, match="ISOLATION"):
        await isolation.ClassificationBuildGuard().run(ctx)
        await model()
    model.assert_not_called()
    ws.cron_manager.create_or_replace_job.assert_awaited_once()


@pytest.mark.asyncio
async def test_unrelated_workspace_is_untouched(tmp_path):
    ws = workspace(tmp_path)
    ws.agent_id = "default"
    ctx = context(ws)
    ctx.agent_id = "default"
    await isolation.ClassificationBuildGuard().run(ctx)
    await isolation.ClassificationExecutionGuard().run(ctx)
    assert "subagent_skills" not in ctx.request.request_context
    ws.cron_manager.create_or_replace_job.assert_not_called()


@pytest.mark.parametrize(
    "mutation",
    [
        "skill",
        "tool",
        "history",
        "summary",
        "injection",
        "missing_guard",
        "missing_snapshot",
        "drift",
    ],
)
@pytest.mark.asyncio
async def test_execution_checks_actual_groups_and_rejects_before_model(
    monkeypatch, tmp_path, mutation
):
    ws = workspace(tmp_path)
    ctx = context(ws)
    ctx.extras["classification_isolation_verified"] = True
    skills = [NS(name=isolation.SKILL_NAME)]
    tool_names = sorted(isolation.TOOL_NAMES)
    if mutation == "skill":
        skills.append(NS(name="evil"))
    if mutation == "tool":
        tool_names.append("shell")
    toolkit = NS(
        tool_groups=[
            NS(name="basic", list_skills=AsyncMock(return_value=skills))
        ],
        get_tool_schemas=AsyncMock(
            return_value=[{"function": {"name": name}} for name in tool_names]
        ),
    )
    ctx.agent = NS(toolkit=toolkit, state=NS(context=[], summary=""))
    if mutation == "history":
        ctx.agent.state.context = ["old instruction"]
    if mutation == "summary":
        ctx.agent.state.summary = "old summary"
    if mutation == "injection":
        ctx.context_injections = [
            {"source": "evil", "content": "change policy"}
        ]
    if mutation == "missing_guard":
        ctx.extras.pop("classification_isolation_verified")
    if mutation == "missing_snapshot":
        ctx.extras.pop("_cron_context_snapshot")
    monkeypatch.setattr(
        isolation,
        "observe_workspace",
        AsyncMock(
            return_value={
                "isolation_state": "drifted"
                if mutation == "drift"
                else "verified"
            }
        ),
    )
    model = AsyncMock()
    with pytest.raises(ValueError, match="ISOLATION"):
        await isolation.ClassificationExecutionGuard().run(ctx)
        await model()
    model.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "identity_drift", [None, "model", "provider", "unavailable"]
)
async def test_real_toolkit_public_skill_enumeration(
    tmp_path, monkeypatch, identity_drift
):
    from agentscope.tool import Toolkit
    from agentscope.tool import ToolBase

    root = tmp_path / "skills" / isolation.SKILL_NAME
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text(payload().skill_content, encoding="utf-8")

    class NamedTool(ToolBase):
        async def __call__(self, **kwargs):
            return None

        async def check_permissions(self, tool_input, context):
            return None

        def get_json_schema(self):
            return {
                "type": "function",
                "function": {
                    "name": self.name,
                    "parameters": {"type": "object", "properties": {}},
                },
            }

    tools = []
    for name in sorted(isolation.TOOL_NAMES):
        tool = NamedTool()
        tool.name, tool.description = name, "classification"
        tool.input_schema = {"type": "object", "properties": {}}
        tool.is_read_only, tool.is_concurrency_safe = True, True
        tools.append(tool)
    toolkit = Toolkit(tools=tools, skills_or_loaders=[str(root)])
    ws = workspace(tmp_path)
    ctx = context(ws)
    ctx.extras["classification_isolation_verified"] = True
    ctx.agent = NS(
        toolkit=toolkit,
        state=NS(context=[], summary=""),
        model=NS(
            get_classification_runtime_model_identity=lambda: {
                "provider_id": "approved",
                "model_id": "test-model",
            }
        ),
    )
    monkeypatch.setattr(
        isolation,
        "observe_workspace",
        AsyncMock(
            return_value={
                "isolation_state": "verified",
                "provider_id": "approved",
                "model_id": "test-model",
            }
        ),
    )
    assert [
        skill.name
        for group in toolkit.tool_groups
        for skill in await group.list_skills()
    ] == [isolation.SKILL_NAME]
    if identity_drift:
        actual = {"provider_id": "approved", "model_id": "test-model"}
        if identity_drift == "model":
            actual["model_id"] = "changed"
        if identity_drift == "provider":
            actual["provider_id"] = "changed"
        ctx.agent.model.get_classification_runtime_model_identity = (
            lambda: None if identity_drift == "unavailable" else actual
        )
        model = AsyncMock()
        with pytest.raises(ValueError, match="ISOLATION"):
            await isolation.ClassificationExecutionGuard().run(ctx)
            await model()
        model.assert_not_called()
        return
    await isolation.ClassificationExecutionGuard().run(ctx)
    assert {
        schema["function"]["name"]
        for schema in await ctx.agent.toolkit.get_tool_schemas()
    } == isolation.TOOL_NAMES
    with pytest.raises(ValueError, match="TOOL_REJECTED"):
        async for _ in ctx.agent.toolkit.call_tool(NS(name="Skill"), NS()):
            pass


@pytest.mark.asyncio
async def test_native_history_isolated_then_restored(tmp_path):
    from qwenpaw.hooks.cron.cron_hook import (
        CronMemoryIsolateHook,
        CronMemoryRestoreHook,
    )

    ctx = context(workspace(tmp_path))
    ctx.agent = NS(
        state=NS(context=["malicious old instruction"], summary="old summary")
    )
    await CronMemoryIsolateHook().run(ctx)
    assert ctx.agent.state.context == [] and ctx.agent.state.summary == ""
    ctx.agent.state.context.append("this round")
    await CronMemoryRestoreHook().run(ctx)
    assert ctx.agent.state.context == [
        "malicious old instruction",
        "this round",
    ]


def test_unknown_parameters_are_not_a_fake_profile_hash():
    assert isolation.parameter_observation(NS()) == {
        "parameters_state": "unknown",
        "effective_parameters_sha256": None,
    }


@pytest.mark.asyncio
async def test_spoofed_json_cron_source_has_no_trusted_provenance(
    monkeypatch, tmp_path
):
    ctx = context(workspace(tmp_path))
    monkeypatch.setattr(
        isolation,
        "observe_workspace",
        AsyncMock(return_value={"isolation_state": "verified"}),
    )
    assert isolation.get_cron_execution_identity() is None
    with pytest.raises(ValueError, match="ISOLATION"):
        await isolation.ClassificationBuildGuard().run(ctx)


@pytest.mark.asyncio
async def test_executor_provenance_is_scoped_and_resets_on_failure(tmp_path):
    from qwenpaw.app.crons.executor import CronExecutor
    from qwenpaw.app.crons.execution_context import get_cron_execution_identity

    ws = workspace(tmp_path)
    executor = CronExecutor(workspace=ws, channel_manager=object())

    async def observe(job):
        identity = get_cron_execution_identity()
        assert (
            identity.workspace is ws and identity.job_id == isolation.AGENT_ID
        )
        return {"status": "success"}

    executor._execute = observe
    assert await executor.execute(build_job(payload())) == {
        "status": "success"
    }
    assert get_cron_execution_identity() is None

    async def fail(job):
        raise ValueError("controlled failure")

    executor._execute = fail
    with pytest.raises(ValueError):
        await executor.execute(build_job(payload()))
    assert get_cron_execution_identity() is None


@pytest.fixture
def native_sync_environment(monkeypatch, tmp_path):
    import qwenpaw.config.config as config
    import qwenpaw.config.utils as utils
    import qwenpaw.app.mcp.config_service as mcp
    import bank_runtime.classification_operations as operations

    ws = workspace(tmp_path)
    ws.cron_manager.get_job.return_value = None

    async def save_job(job):
        ws.cron_manager.get_job.return_value = job

    ws.cron_manager.create_or_replace_job.side_effect = save_job
    tool_defaults = {
        "execute_shell_command": config.BuiltinToolConfig(
            name="execute_shell_command", enabled=True
        )
    }
    monkeypatch.setattr(config, "_BUILTIN_TOOLS_CACHE", tool_defaults)
    monkeypatch.setattr(config, "_merge_plugin_manifest_tools", lambda _: None)
    profile = config.AgentProfileConfig(
        id=isolation.AGENT_ID,
        name="Classification",
        workspace_dir=str(tmp_path),
        tools=config.ToolsConfig(builtin_tools=tool_defaults),
    )
    profile.active_model = config.ModelSlotConfig(
        provider_id="approved", model="test-model"
    )
    stored = [profile]
    global_config = NS(
        agents=NS(
            profiles={
                isolation.AGENT_ID: config.AgentProfileRef(
                    id=isolation.AGENT_ID, workspace_dir=str(tmp_path)
                )
            },
            agent_order=[],
        )
    )
    monkeypatch.setattr(utils, "load_config", lambda: global_config)
    monkeypatch.setattr(utils, "save_config", lambda _: None)
    monkeypatch.setattr(config, "load_agent_config", lambda _: stored[0])
    monkeypatch.setattr(
        config,
        "save_agent_config",
        lambda _, value: stored.__setitem__(0, value),
    )
    policy = [None]

    class FakeMCP:
        def __init__(self, workspace):
            pass

        async def list_cards(self):
            return []

        async def create_client(self, *args):
            pass

        async def update_client(self, *args):
            pass

        async def wait_for_reloads(self):
            pass

        async def update_policy(self, _, value):
            policy[0] = value

        async def get_policy(self, _):
            return policy[0]

        async def list_tools(self, _):
            return [
                NS(name="claim_tasks", enabled=True),
                NS(name="submit_classification", enabled=True),
            ]

    monkeypatch.setattr(mcp, "MCPConfigService", FakeMCP)
    monkeypatch.setattr(operations, "verify_mapping_proof", AsyncMock())
    monkeypatch.setattr(
        isolation,
        "parameter_observation",
        lambda _: {
            "parameters_state": "verified",
            "effective_parameters_sha256": "f" * 64,
        },
    )
    manager = NS(
        get_agent=AsyncMock(return_value=ws), reload_agent=AsyncMock()
    )
    return manager, ws, stored


@pytest.mark.asyncio
async def test_native_sync_retry_recovery_and_actual_status(
    native_sync_environment, tmp_path
):
    from bank_runtime.classification_operations import (
        sync_mapping,
        classification_status,
    )

    manager, ws, _ = native_sync_environment
    first = await sync_mapping(manager, payload())
    assert (
        first["isolation_state"] == "verified"
        and first["cron_enabled"] is True
    )
    assert (
        first["skills"] == [isolation.SKILL_NAME]
        and set(first["tools"]) == isolation.TOOL_NAMES
    )
    calls = ws.cron_manager.create_or_replace_job.await_count
    retry = await sync_mapping(manager, payload())
    assert retry["skill_sha256"] == first["skill_sha256"]
    assert ws.cron_manager.create_or_replace_job.await_count == calls
    recovered = await sync_mapping(manager, payload(publication_epoch=2))
    assert recovered["publication_epoch"] == 2
    assert (await classification_status(manager))["publication_epoch"] == 2
    with pytest.raises(ValueError, match="stale"):
        await sync_mapping(manager, payload(publication_epoch=1))
    assert ws.cron_manager.get_job.return_value.enabled is True
    (tmp_path / "AGENTS.md").write_text("tampered prompt", encoding="utf-8")
    assert (await classification_status(manager))[
        "isolation_state"
    ] == "drifted"


@pytest.mark.asyncio
async def test_partial_sync_failure_stops_native_cron(
    native_sync_environment, monkeypatch
):
    from bank_runtime.classification_operations import sync_mapping

    manager, ws, _ = native_sync_environment
    await sync_mapping(manager, payload())
    monkeypatch.setattr(
        isolation,
        "verify_hook_dependencies",
        lambda _: (_ for _ in ()).throw(ValueError("missing hook")),
    )
    with pytest.raises(ValueError, match="missing hook"):
        await sync_mapping(
            manager,
            payload(
                operation_id="op-2",
                publication_epoch=2,
                mapping_version="v2",
                expected_skill_sha256=payload().skill_sha256,
            ),
        )
    assert ws.cron_manager.get_job.return_value.enabled is False


@pytest.mark.asyncio
async def test_evaluation_keeps_cron_disabled(
    native_sync_environment, monkeypatch
):
    from bank_runtime.classification_operations import sync_mapping

    manager, ws, _ = native_sync_environment
    monkeypatch.setenv("QWENPAW_CLASSIFICATION_ENVIRONMENT", "test")
    result = await sync_mapping(
        manager, payload(execution_mode="manual_evaluation")
    )
    assert (
        result["cron_enabled"] is False
        and result["execution_mode"] == "manual_evaluation"
    )
    assert ws.cron_manager.get_job.return_value.enabled is False


def test_explicit_public_provider_parameter_proof(monkeypatch):
    from qwenpaw.providers import ProviderManager

    evidence = dict(
        source_version="adapter-1",
        model_revision="model-revision-1",
        gateway_revision="gateway-1",
        parameters={
            "temperature": 0.0,
            "top_p": 1.0,
            "max_tokens": 4096,
            "seed": None,
        },
    )
    provider = NS(get_classification_parameter_evidence=lambda _: evidence)
    monkeypatch.setattr(
        ProviderManager,
        "get_instance",
        lambda: NS(get_provider=lambda _: provider),
    )
    profile = NS(active_model=NS(provider_id="approved", model="test-model"))
    result = isolation.parameter_observation(profile)
    assert result["parameters_state"] == "verified"
    assert len(result["effective_parameters_sha256"]) == 64
    evidence["parameters"]["temperature"] = float("nan")
    assert (
        isolation.parameter_observation(profile)["parameters_state"]
        == "unknown"
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("publication_epoch", True),
        ("publication_epoch", "1"),
        ("interval_minutes", True),
        ("interval_minutes", "5"),
        ("enabled", 1),
    ],
)
def test_mapping_rejects_coercion(field, value):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        payload(**{field: value})


@pytest.mark.asyncio
async def test_unknown_parameters_sync_content_without_enabling_production(
    native_sync_environment, monkeypatch
):
    from bank_runtime.classification_operations import sync_mapping

    manager, ws, _ = native_sync_environment
    monkeypatch.setattr(
        isolation,
        "parameter_observation",
        lambda _: {
            "parameters_state": "unknown",
            "effective_parameters_sha256": None,
        },
    )
    result = await sync_mapping(manager, payload())
    assert (
        result["parameters_state"] == "unknown"
        and result["cron_enabled"] is False
    )
    assert ws.cron_manager.get_job.return_value.enabled is False


@pytest.mark.asyncio
async def test_parameter_drift_is_not_healthy(
    native_sync_environment, monkeypatch
):
    from bank_runtime.classification_operations import (
        sync_mapping,
        classification_status,
    )

    manager, _, _ = native_sync_environment
    await sync_mapping(manager, payload())
    monkeypatch.setattr(
        isolation,
        "parameter_observation",
        lambda _: {
            "parameters_state": "verified",
            "effective_parameters_sha256": "b" * 64,
        },
    )
    assert (await classification_status(manager))[
        "isolation_state"
    ] == "drifted"


@pytest.mark.asyncio
async def test_disable_checks_exact_operation_before_stopping(
    native_sync_environment, monkeypatch
):
    from bank_runtime.classification_operations import (
        sync_mapping,
        build_classification_router,
    )
    from fastapi import FastAPI
    import httpx

    manager, ws, _ = native_sync_environment
    await sync_mapping(manager, payload())
    monkeypatch.setenv("QWENPAW_SERVICE_TOKEN", "test-service-only")
    app = FastAPI()
    app.state.multi_agent_manager = manager
    app.include_router(build_classification_router())
    body = dict(
        mapping_version="v1",
        skill_sha256=payload().skill_sha256,
        operation_id="op-1",
        publication_epoch=1,
    )
    headers = {
        "Authorization": "Bearer test-service-only",
        "X-Agent-Id": isolation.AGENT_ID,
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (
            await client.post("/classification-operations/disable", json=body)
        ).status_code == 401
        assert (
            await client.post(
                "/classification-operations/disable",
                json=body | {"publication_epoch": True},
                headers=headers,
            )
        ).status_code == 422
        assert (
            await client.post(
                "/classification-operations/disable",
                json=body | {"operation_id": "old"},
                headers=headers,
            )
        ).status_code == 409
        assert ws.cron_manager.get_job.return_value.enabled is True
        stopped = await client.post(
            "/classification-operations/disable", json=body, headers=headers
        )
        assert (
            stopped.status_code == 200
            and stopped.json()["cron_enabled"] is False
        )
        assert ws.cron_manager.get_job.return_value.enabled is False
        assert (
            await client.post(
                "/classification-operations/disable",
                json=body,
                headers=headers,
            )
        ).status_code == 200


@pytest.mark.asyncio
async def test_fresh_status_observes_default_model_without_creating_agent(
    monkeypatch,
):
    from bank_runtime import classification_operations as operations
    from qwenpaw.providers import ProviderManager
    from qwenpaw.config import utils

    model = NS(provider_id="approved", model="approved-model")
    monkeypatch.setattr(
        utils, "load_config", lambda: NS(agents=NS(profiles={}))
    )
    monkeypatch.setattr(
        ProviderManager,
        "get_instance",
        lambda: NS(get_active_model=lambda: model),
    )
    monkeypatch.setattr(
        isolation,
        "parameter_observation",
        lambda profile: {
            "parameters_state": "verified",
            "effective_parameters_sha256": "a" * 64,
        },
    )
    manager = NS(
        get_agent=AsyncMock(
            side_effect=AssertionError("must not create workspace")
        )
    )
    observed = await operations.classification_status(manager)
    assert observed["isolation_state"] == "unconfigured"
    assert observed["model_id"] == "approved-model"
    assert observed["skills"] == observed["tools"] == []
    assert (
        observed["skill_sha256"] == ""
        and observed["publication_epoch"] is None
    )
    assert observed["parameters_state"] == "verified"
    manager.get_agent.assert_not_called()


@pytest.mark.asyncio
async def test_evaluation_run_requires_test_instance(
    native_sync_environment, monkeypatch
):
    from bank_runtime.classification_operations import (
        sync_mapping,
        build_classification_router,
    )
    from fastapi import FastAPI
    import httpx

    manager, ws, _ = native_sync_environment
    monkeypatch.setenv("QWENPAW_CLASSIFICATION_ENVIRONMENT", "test")
    await sync_mapping(manager, payload(execution_mode="manual_evaluation"))
    assert ws.cron_manager.get_job.return_value.enabled is False
    ws.cron_manager.run_job = AsyncMock()
    monkeypatch.setenv("QWENPAW_SERVICE_TOKEN", "test-service-only")
    monkeypatch.setenv("QWENPAW_CLASSIFICATION_OPERATIONS_ENABLED", "1")
    app = FastAPI()
    app.state.multi_agent_manager = manager
    app.include_router(build_classification_router())
    headers = {
        "Authorization": "Bearer test-service-only",
        "X-Agent-Id": isolation.AGENT_ID,
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (
            await client.post(
                "/classification-operations/run", headers=headers
            )
        ).status_code == 200
        monkeypatch.delenv("QWENPAW_CLASSIFICATION_ENVIRONMENT")
        assert (
            await client.post(
                "/classification-operations/run", headers=headers
            )
        ).status_code == 409
    ws.cron_manager.run_job.assert_awaited_once()


@pytest.mark.asyncio
async def test_status_reads_current_model_instead_of_receipt(
    native_sync_environment,
):
    from bank_runtime.classification_operations import (
        sync_mapping,
        classification_status,
    )

    manager, _, stored = native_sync_environment
    await sync_mapping(manager, payload())
    stored[0].active_model.model = "changed-model"
    observed = await classification_status(manager)
    assert observed["model_id"] == "changed-model"
    assert observed["isolation_state"] == "drifted"
    assert observed["last_run_at"] == observed["cron_state"]["last_run_at"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "epoch,valid", [(True, False), ("1", False), (1, True)]
)
async def test_mapping_proof_epoch_is_strict(monkeypatch, epoch, valid):
    import httpx
    from bank_runtime import classification_operations as operations

    request = payload()
    proof = dict(
        runtime_url=request.runtime_base_url,
        tenant_id=request.runtime_tenant_id,
        agent_id=isolation.AGENT_ID,
        mapping_version=request.mapping_version,
        skill_sha256=request.skill_sha256,
        operation_id=request.operation_id,
        publication_epoch=epoch,
    )
    original = httpx.AsyncClient
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json=proof))
    monkeypatch.setattr(
        operations.httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=transport, **kwargs),
    )
    if valid:
        await operations.verify_mapping_proof(request)
    else:
        with pytest.raises(ValueError, match="integer"):
            await operations.verify_mapping_proof(request)


@pytest.mark.asyncio
async def test_tenant_receipt_matches_native_skill_manifest(
    native_sync_environment, tmp_path
):
    from bank_runtime.classification_operations import (
        sync_mapping,
        classification_status,
    )

    manager, _, _ = native_sync_environment
    observed = await sync_mapping(manager, payload())
    assert observed["tenant_id"] == payload().runtime_tenant_id
    receipt_file = tmp_path / "classification-mapping.json"
    value = json.loads(receipt_file.read_text())
    value["tenant_id"] = "other-tenant"
    receipt_file.write_text(json.dumps(value))
    assert (await classification_status(manager))[
        "isolation_state"
    ] == "drifted"


def test_native_model_identity_reads_called_model_through_both_wrappers():
    from agentscope.credential import OpenAICredential
    from agentscope.model import OpenAIChatModel
    from qwenpaw.providers.openai_chat_model_compat import (
        OpenAIChatModelCompat,
    )
    from qwenpaw.token_usage.model_wrapper import TokenRecordingModelWrapper
    from qwenpaw.providers.retry_chat_model import RetryChatModel

    native = OpenAIChatModelCompat(
        credential=OpenAICredential(
            id="qwenpaw-approved", api_key="synthetic-only"
        ),
        model="model-a",
        parameters=OpenAIChatModel.Parameters(),
        provider_id="approved",
    )
    accounting = TokenRecordingModelWrapper("approved", native)
    retry = RetryChatModel(accounting)
    assert retry.get_classification_runtime_model_identity() == {
        "provider_id": "approved",
        "model_id": "model-a",
    }
    native.model = "changed-model"
    assert (
        retry.get_classification_runtime_model_identity()["model_id"]
        == "changed-model"
    )
    assert retry.model == "model-a"
    native.bind_qwenpaw_provider_id("other-provider")
    with pytest.raises(ValueError, match="IDENTITY_DRIFT"):
        retry.get_classification_runtime_model_identity()


@pytest.fixture
def actual_request_provider(monkeypatch):
    from qwenpaw.providers.openai_provider import OpenAIProvider
    from qwenpaw.providers import ProviderManager

    provider = OpenAIProvider(
        id="approved",
        name="Synthetic",
        base_url="https://mock-provider.invalid/v1",
        api_key="synthetic-key",
    )
    monkeypatch.setattr(
        ProviderManager,
        "get_instance",
        lambda: NS(get_provider=lambda _: provider),
    )
    return provider


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "qenv,qpolicy,peer_env,peer_policy,enabled",
    [
        ("development", "local_wire_v1", "development", "local_wire_v1", True),
        ("test", "local_wire_v1", "test", "local_wire_v1", True),
        ("production", "local_wire_v1", "production", "local_wire_v1", False),
        ("development", "", "development", "local_wire_v1", False),
        ("development", "local_wire_v1", "test", "local_wire_v1", False),
        ("development", "local_wire_v1", None, "provider_verified", False),
    ],
)
async def test_local_request_policy_requires_both_process_and_peer_opt_in(
    native_sync_environment,
    actual_request_provider,
    monkeypatch,
    qenv,
    qpolicy,
    peer_env,
    peer_policy,
    enabled,
):
    from bank_runtime.classification_operations import sync_mapping

    manager, ws, _ = native_sync_environment
    monkeypatch.setattr(
        isolation,
        "parameter_observation",
        lambda _: {
            "parameters_state": "unknown",
            "effective_parameters_sha256": None,
        },
    )
    monkeypatch.setenv("QWENPAW_CLASSIFICATION_ENVIRONMENT", qenv)
    monkeypatch.setenv("QWENPAW_CLASSIFICATION_REQUEST_POLICY", qpolicy)
    result = await sync_mapping(
        manager,
        payload(runtime_environment=peer_env, request_policy=peer_policy),
    )
    assert result["cron_enabled"] is enabled
    assert result["request_policy"] == (
        "local_wire_v1" if enabled else "provider_verified"
    )
    assert result["request_parameters_state"] == "verified"
    assert (
        result["parameters_state"] == "unknown"
        and result["effective_parameters_sha256"] is None
    )
    assert ws.cron_manager.get_job.return_value.enabled is enabled


@pytest.mark.asyncio
async def test_local_policy_drift_and_production_switch_rejected(
    native_sync_environment, actual_request_provider, monkeypatch
):
    from bank_runtime.classification_operations import (
        sync_mapping,
        classification_status,
    )

    manager, ws, _ = native_sync_environment
    monkeypatch.setattr(
        isolation,
        "parameter_observation",
        lambda _: {
            "parameters_state": "unknown",
            "effective_parameters_sha256": None,
        },
    )
    monkeypatch.setenv("QWENPAW_CLASSIFICATION_ENVIRONMENT", "development")
    monkeypatch.setenv(
        "QWENPAW_CLASSIFICATION_REQUEST_POLICY", "local_wire_v1"
    )
    await sync_mapping(
        manager,
        payload(
            runtime_environment="development", request_policy="local_wire_v1"
        ),
    )
    actual_request_provider.generate_kwargs["temperature"] = 0.7
    assert (await classification_status(manager))[
        "isolation_state"
    ] == "drifted"
    actual_request_provider.generate_kwargs.clear()
    monkeypatch.setenv("QWENPAW_CLASSIFICATION_ENVIRONMENT", "production")
    result = await classification_status(manager)
    assert (
        result["isolation_state"] == "drifted"
        and result["request_policy"] == "provider_verified"
    )
    ctx = context(ws)
    monkeypatch.setattr(
        isolation,
        "get_cron_execution_identity",
        lambda: NS(workspace=ws, job_id=isolation.AGENT_ID),
    )
    model = AsyncMock()
    with pytest.raises(ValueError, match="ISOLATION"):
        await isolation.ClassificationBuildGuard().run(ctx)
        await model()
    model.assert_not_called()
    assert ws.cron_manager.get_job.return_value.enabled is False


@pytest.mark.asyncio
async def test_provider_proof_priority_and_eval_rejects_local_policy(
    native_sync_environment, actual_request_provider, monkeypatch
):
    from bank_runtime.classification_operations import sync_mapping

    manager, _, _ = native_sync_environment
    monkeypatch.setenv("QWENPAW_CLASSIFICATION_ENVIRONMENT", "test")
    monkeypatch.setenv(
        "QWENPAW_CLASSIFICATION_REQUEST_POLICY", "local_wire_v1"
    )
    result = await sync_mapping(
        manager,
        payload(runtime_environment="test", request_policy="local_wire_v1"),
    )
    assert (
        result["parameters_state"] == "verified"
        and result["request_policy"] == "provider_verified"
    )
    result = await sync_mapping(
        manager,
        payload(
            operation_id="op-eval",
            publication_epoch=2,
            expected_skill_sha256=payload().skill_sha256,
            execution_mode="manual_evaluation",
            runtime_environment="test",
            request_policy="local_wire_v1",
        ),
    )
    assert (
        result["request_policy"] == "provider_verified"
        and result["cron_enabled"] is False
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("broken", [False, True])
async def test_native_persisted_cron_starts_only_after_classification_guards(
    tmp_path, monkeypatch, broken
):
    from qwenpaw.app.crons.manager import CronManager
    import qwenpaw.app.crons.manager as cron_module
    import asyncio
    from qwenpaw.app.crons.repo.json_repo import JsonJobRepository
    from qwenpaw.app.multi_agent_manager import MultiAgentManager
    from qwenpaw.plugins.registry import PluginRegistry
    import qwenpaw.app.multi_agent_manager as module

    previous = PluginRegistry._instance
    PluginRegistry._instance = None
    try:
        plugin_registry = PluginRegistry()
        plugin_registry.register_workspace_starting_hook(
            "bank-runtime",
            "classification-before-start",
            isolation.classification_workspace_starting,
        )
        ws = workspace(tmp_path)
        # Native workspace bootstrap provides built-ins but not plugin guards.
        ws.plugins.hook_registry = registry()
        for phase in (Phase.PRE_AGENT_BUILD, Phase.PRE_EXECUTE):
            ws.plugins.hook_registry._by_phase[phase] = [
                h
                for h in ws.plugins.hook_registry._by_phase[phase]
                if not h.name.startswith("classification_")
            ]
        if broken:
            ws.plugins.hook_registry._by_phase[Phase.PRE_EXECUTE] = []
        repo = JsonJobRepository(str(tmp_path / "jobs.json"))
        await repo.upsert_job(build_job(payload()))
        cron = CronManager(
            repo=repo,
            workspace=ws,
            channel_manager=None,
            agent_id=isolation.AGENT_ID,
        )
        ws.cron_manager = cron
        scheduler_start = cron._scheduler.start
        events = []

        def start_scheduler():
            isolation.verify_hook_dependencies(ws)
            events.append("scheduler")
            scheduler_start()

        monkeypatch.setattr(cron._scheduler, "start", start_scheduler)
        monkeypatch.setattr(
            cron_module, "get_heartbeat_config", lambda _: NS(enabled=False)
        )
        monkeypatch.setattr(cron, "_register_memory_jobs", lambda: None)
        model = AsyncMock()
        scheduled = []

        async def execute_due(job):
            if not job.enabled:
                return
            events.append("due")
            ctx = context(workspace(tmp_path))
            ctx.workspace = ws
            monkeypatch.setattr(
                isolation,
                "get_cron_execution_identity",
                lambda: NS(workspace=ws, job_id=isolation.AGENT_ID),
            )
            monkeypatch.setattr(
                isolation,
                "observe_workspace",
                AsyncMock(return_value={"isolation_state": "drifted"}),
            )
            with pytest.raises(ValueError, match="ISOLATION"):
                await isolation.ClassificationBuildGuard().run(ctx)
                await model()

        async def register_due(job):
            if job.enabled:
                scheduled.append(asyncio.create_task(execute_due(job)))

        monkeypatch.setattr(cron, "_register_or_update", register_due)

        async def start_services():
            await cron.start()
            await asyncio.gather(*scheduled)

        ws.start = AsyncMock(side_effect=start_services)
        ws.set_manager = lambda _: None
        manager = MultiAgentManager()
        manager._create_workspace = lambda **_: ws
        monkeypatch.setattr(
            module,
            "load_config",
            lambda: NS(
                agents=NS(
                    profiles={
                        isolation.AGENT_ID: NS(workspace_dir=str(tmp_path))
                    }
                )
            ),
        )
        if broken:
            with pytest.raises(ValueError):
                await manager.get_agent(isolation.AGENT_ID)
            ws.start.assert_not_awaited()
            assert events == [] and not cron._started
        else:
            await manager.get_agent(isolation.AGENT_ID)
            assert events[:2] == ["scheduler", "due"]
        model.assert_not_called()
        await cron.stop()
    finally:
        PluginRegistry._instance = previous


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "drift", [None, "receipt", "skill", "agents", "job", "profile"]
)
async def test_legacy_starting_declaration_restore_only_updates_native_profile(
    native_sync_environment, tmp_path, monkeypatch, drift
):
    from bank_runtime.classification_operations import sync_mapping
    from bank_runtime.classification_isolation import (
        restore_starting_hook_requirement,
        STARTING_HOOK,
    )

    manager, ws, stored = native_sync_environment
    await sync_mapping(manager, payload())
    stored[0].required_starting_hooks = []
    (tmp_path / "agent.json").write_text("{}", encoding="utf-8")
    (tmp_path / "jobs.json").write_text(
        json.dumps(
            {
                "version": 1,
                "jobs": [
                    ws.cron_manager.get_job.return_value.model_dump(
                        mode="json"
                    )
                ],
            }
        ),
        encoding="utf-8",
    )
    files = [
        tmp_path / "classification-mapping.json",
        tmp_path / "jobs.json",
        tmp_path / "skills" / isolation.SKILL_NAME / "SKILL.md",
    ]
    if drift == "receipt":
        receipt = json.loads(files[0].read_text())
        receipt["mapping_version"] = "other"
        files[0].write_text(json.dumps(receipt))
    if drift == "skill":
        files[2].write_text("changed")
    if drift == "agents":
        (tmp_path / "AGENTS.md").write_text("changed")
    if drift == "job":
        jobs = json.loads(files[1].read_text())
        jobs["jobs"][0]["enabled"] = False
        files[1].write_text(json.dumps(jobs))
    if drift == "profile":
        stored[0].active_model.model = "other"
    before = {file: file.read_bytes() for file in files}
    if drift:
        with pytest.raises(ValueError, match="RESTORE_METADATA_DRIFT"):
            restore_starting_hook_requirement()
        assert stored[0].required_starting_hooks == []
    else:
        assert restore_starting_hook_requirement() == {
            "state": "verified",
            "changed": True,
        }
        assert stored[0].required_starting_hooks == [STARTING_HOOK]
        assert restore_starting_hook_requirement() == {
            "state": "verified",
            "changed": False,
        }
    assert before == {file: file.read_bytes() for file in files}
