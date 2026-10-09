from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from qwenpaw.plugins.api import PluginApi
from qwenpaw.plugins.registry import PluginRegistry
from qwenpaw.app.multi_agent_manager import MultiAgentManager


@pytest.fixture
def fresh_registry():
    previous = PluginRegistry._instance
    PluginRegistry._instance = None
    try:
        yield PluginRegistry()
    finally:
        PluginRegistry._instance = previous


@pytest.mark.asyncio
async def test_starting_api_uses_actual_unstarted_workspace_and_priority(
    fresh_registry,
):
    api = PluginApi("test-plugin", config={})
    api.set_registry(fresh_registry)
    observed = []
    workspace = SimpleNamespace(agent_id="test", workspace_dir="/tmp/test")

    async def first(info):
        assert info["workspace"] is workspace
        observed.append("first")

    api.register_workspace_starting_hook(
        "later", lambda info: observed.append("later"), priority=20
    )
    api.register_workspace_starting_hook("first", first, priority=10)
    await MultiAgentManager._fire_workspace_starting_hooks(workspace)
    assert observed == ["first", "later"]
    fresh_registry.remove_hooks_by_name("test-plugin", ["first"])
    assert [
        h.hook_name for h in fresh_registry.get_workspace_starting_hooks()
    ] == ["later"]
    fresh_registry.unregister_plugin("test-plugin")
    assert fresh_registry.get_workspace_starting_hooks() == []


@pytest.mark.asyncio
async def test_starting_hook_exception_is_not_swallowed(fresh_registry):
    failure = RuntimeError("prerequisite unavailable")
    hook = AsyncMock(side_effect=failure)
    fresh_registry.register_workspace_starting_hook("test", "required", hook)
    with pytest.raises(RuntimeError) as error:
        await MultiAgentManager._fire_workspace_starting_hooks(
            SimpleNamespace(agent_id="test", workspace_dir="/tmp/test")
        )
    assert error.value is failure


def test_starting_api_without_registry_fails_closed():
    api = PluginApi("test", config={})
    with pytest.raises(RuntimeError, match="registry"):
        api.register_workspace_starting_hook("required", lambda _: None)


@pytest.mark.asyncio
async def test_preinstalled_hook_created_and_startup_are_idempotent(
    fresh_registry,
):
    from qwenpaw.runtime.hooks import HookBase, HookRegistry
    from qwenpaw.runtime.phases import Phase

    class Required(HookBase):
        name = "required"
        phase = Phase.PRE_AGENT_BUILD

    api = PluginApi("test", config={})
    api.set_registry(fresh_registry)
    workspace = SimpleNamespace(
        agent_id="test",
        workspace_dir="/tmp/test",
        plugins=SimpleNamespace(hook_registry=HookRegistry()),
    )
    fresh_registry.set_workspace_manager(
        SimpleNamespace(agents={"test": workspace})
    )
    installed = Required()
    workspace.plugins.hook_registry.register(installed)
    api.register_runtime_hook(Required(), if_missing=True)
    await MultiAgentManager._fire_workspace_created_hooks(
        {"agent_id": "test", "workspace": workspace}
    )
    for registration in fresh_registry.get_startup_hooks():
        registration.callback()
    assert workspace.plugins.hook_registry.hooks_for(
        Phase.PRE_AGENT_BUILD
    ) == [installed]


@pytest.mark.parametrize(
    "value",
    [
        None,
        "plugin:hook",
        [1],
        [["nested"]],
        ["missing-colon"],
        ["p:h", "p:h"],
    ],
)
def test_required_hook_declaration_is_strict(value):
    from pydantic import ValidationError
    from qwenpaw.config.config import AgentProfileConfig

    with pytest.raises(ValidationError):
        AgentProfileConfig(
            id="test", name="test", required_starting_hooks=value
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("completed", [False, True])
async def test_workspace_services_require_actual_completed_hook(
    tmp_path, monkeypatch, completed
):
    import qwenpaw.app.workspace.workspace as module
    from qwenpaw.app.workspace import Workspace
    from qwenpaw.config.config import AgentProfileConfig
    from qwenpaw.agents import skill_system

    monkeypatch.setattr(
        skill_system, "ensure_skill_pool_initialized", lambda: None
    )
    monkeypatch.setattr(
        module,
        "load_agent_config",
        lambda _: AgentProfileConfig(
            id="test", name="test", required_starting_hooks=["p:required"]
        ),
    )
    ws = Workspace(agent_id="test", workspace_dir=str(tmp_path))
    ws._migrate_legacy_weixin_data = lambda: None
    ws._service_manager.start_all = AsyncMock()
    ws._service_manager.stop_all = AsyncMock()
    if completed:
        ws._completed_starting_hooks.add("p:required")
        await ws.start()
        ws._service_manager.start_all.assert_awaited_once()
    else:
        with pytest.raises(RuntimeError, match="did not complete"):
            await ws.start()
        ws._service_manager.start_all.assert_not_awaited()
    await ws.stop()


def test_other_workspace_default_has_no_new_requirement():
    from qwenpaw.config.config import AgentProfileConfig

    assert (
        AgentProfileConfig(
            id="default", name="Default"
        ).required_starting_hooks
        == []
    )
