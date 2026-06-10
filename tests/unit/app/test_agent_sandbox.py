# -*- coding: utf-8 -*-
"""Tests for agent sandbox profile wiring."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from qwenpaw.app.routers import agents as agents_router
from qwenpaw.config.config import (
    AgentProfileRef,
    Config,
    SandboxProfileConfig,
    load_sandbox_profile_definitions,
)


@pytest.mark.asyncio
async def test_create_agent_with_bubblewrap_profile_switches_shell_tools(
    monkeypatch,
    tmp_path,
):
    """Sandboxed agents should use only the sandboxed shell tool."""
    config = Config()
    config.agents.profiles = {
        "default": AgentProfileRef(id="default", workspace_dir="/tmp/default"),
    }
    config.agents.agent_order = ["default"]
    saved: dict[str, object] = {}

    monkeypatch.setattr(agents_router, "load_config", lambda: config)
    monkeypatch.setattr(agents_router, "save_config", lambda updated: None)
    monkeypatch.setattr(
        agents_router,
        "save_agent_config",
        lambda agent_id, agent_config: saved.setdefault(
            "agent_config",
            agent_config,
        ),
    )
    monkeypatch.setattr(
        agents_router,
        "_initialize_agent_workspace",
        lambda workspace_dir, skill_names=None, md_template_id=None, language=None: None,  # noqa: E501  # pylint: disable=line-too-long
    )

    await agents_router.create_agent(
        agents_router.CreateAgentRequest(
            id="sandboxed",
            name="Sandboxed",
            workspace_dir=str(tmp_path / "sandboxed"),
            sandbox_profile_id="linux-bubblewrap-workspace",
        ),
    )

    agent_config = saved["agent_config"]
    assert isinstance(agent_config.sandbox, SandboxProfileConfig)
    assert agent_config.sandbox.enabled is True
    assert agent_config.sandbox.profile_id == "linux-bubblewrap-workspace"
    tools = agent_config.tools.builtin_tools
    assert tools["execute_shell_command"].enabled is False
    assert tools["execute_sandboxed_shell_command"].enabled is True


@pytest.mark.asyncio
async def test_create_agent_defaults_to_native_shell(monkeypatch, tmp_path):
    """Native agents should keep the legacy shell tool behavior."""
    config = Config()
    config.agents.profiles = {}
    config.agents.agent_order = []
    saved = SimpleNamespace(agent_config=None)

    monkeypatch.setattr(agents_router, "load_config", lambda: config)
    monkeypatch.setattr(agents_router, "save_config", lambda updated: None)
    monkeypatch.setattr(
        agents_router,
        "save_agent_config",
        lambda agent_id, agent_config: setattr(
            saved,
            "agent_config",
            agent_config,
        ),
    )
    monkeypatch.setattr(
        agents_router,
        "_initialize_agent_workspace",
        lambda workspace_dir, skill_names=None, md_template_id=None, language=None: None,  # noqa: E501  # pylint: disable=line-too-long
    )

    await agents_router.create_agent(
        agents_router.CreateAgentRequest(
            id="native",
            name="Native",
            workspace_dir=str(tmp_path / "native"),
        ),
    )

    tools = saved.agent_config.tools.builtin_tools
    assert saved.agent_config.sandbox.profile_id == "native"
    assert tools["execute_shell_command"].enabled is True
    assert tools["execute_sandboxed_shell_command"].enabled is False


def test_load_sandbox_profiles_from_file(tmp_path):
    """Sandbox profile definitions can live in a standalone JSON file."""
    profiles_file = tmp_path / "sandbox-profiles.json"
    profiles_file.write_text(
        """
        {
          "profiles": [
            {
              "id": "custom-bwrap",
              "name": "Custom bwrap",
              "description": "Custom profile",
              "sandbox": {
                "enabled": true,
                "profile_id": "custom-bwrap",
                "engine": "bubblewrap",
                "allow_network": false,
                "writable_roots": ["{workspace_dir}"],
                "home_dir": "{workspace_dir}/.sandbox/home"
              }
            }
          ]
        }
        """,
        encoding="utf-8",
    )

    profiles = load_sandbox_profile_definitions(profiles_file)

    assert profiles[0].id == "custom-bwrap"
    assert profiles[0].sandbox.engine == "bubblewrap"
