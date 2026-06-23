# -*- coding: utf-8 -*-
"""Tests for the Runtime Tool Gateway bridge used by bank-runtime calls."""
from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest

from qwenpaw.config.context import set_current_runtime_tool_gateway
from qwenpaw.agents.react_agent import (
    QwenPawAgent,
    _build_runtime_tool_gateway_context,
    _runtime_disabled_tools_from_context,
)

runtime_tool_gateway_module = importlib.import_module(
    "qwenpaw.agents.tools.runtime_tool_gateway",
)
runtime_tool_gateway = runtime_tool_gateway_module.runtime_tool_gateway


@pytest.fixture(autouse=True)
def reset_runtime_tool_gateway_context():
    set_current_runtime_tool_gateway(None)
    yield
    set_current_runtime_tool_gateway(None)


def _text(response) -> str:
    texts: list[str] = []
    for part in response.content:
        if isinstance(part, dict):
            texts.append(str(part.get("text", "")))
        else:
            texts.append(str(getattr(part, "text", "")))
    return "\n".join(texts)


def test_build_runtime_tool_gateway_context_guides_controlled_tool_usage() -> None:
    context = _build_runtime_tool_gateway_context(
        {
            "runtime_tool_gateway": {
                "base_url": "http://127.0.0.1:8765",
                "endpoint": "/runtime/v1/tool-calls",
                "task_id": "task_001",
                "allowed_tools": ["workspace.list_outputs"],
            },
            "runtime_constraints": {
                "disabled_tools": ["execute_shell_command", "write_file"],
            },
        }
    )

    assert "runtime_tool_gateway" in context
    assert "workspace.list_outputs" in context
    assert "execute_shell_command" in context
    assert "Do not use native shell" in context
    assert "memory_search" in context


def test_runtime_disabled_tools_from_context_filters_native_tools() -> None:
    disabled = _runtime_disabled_tools_from_context(
        {
            "runtime_constraints": {
                "disabled_tools": ["execute_shell_command", "write_file"],
            }
        }
    )

    assert disabled == {"execute_shell_command", "write_file"}


def test_runtime_gateway_enabled_registers_only_gateway_tool() -> None:
    fake_agent = SimpleNamespace()
    fake_agent._agent_config = SimpleNamespace(
        tools=SimpleNamespace(builtin_tools={}),
    )
    fake_agent._request_context = {
        "runtime_tool_gateway": {
            "base_url": "http://127.0.0.1:8765",
            "endpoint": "/runtime/v1/tool-calls",
            "task_id": "task_001",
            "allowed_tools": ["workspace.list_outputs"],
        },
        "runtime_constraints": {"disabled_tools": []},
    }
    fake_agent._runtime_tool_gateway_enabled = lambda: True
    fake_agent._register_coding_mode_tools = lambda *_, **__: None

    toolkit = QwenPawAgent._create_toolkit(fake_agent, effective_skills=[])

    assert "runtime_tool_gateway" in toolkit.tools
    assert "get_current_time" not in toolkit.tools
    assert "read_file" not in toolkit.tools
    assert "execute_shell_command" not in toolkit.tools


def test_runtime_gateway_empty_allowlist_still_disables_native_tools() -> None:
    fake_agent = SimpleNamespace()
    fake_agent._agent_config = SimpleNamespace(
        tools=SimpleNamespace(builtin_tools={}),
    )
    fake_agent._request_context = {
        "runtime_tool_gateway": {
            "base_url": "http://127.0.0.1:8765",
            "endpoint": "/runtime/v1/tool-calls",
            "task_id": "task_001",
            "allowed_tools": [],
        },
        "runtime_constraints": {"disabled_tools": []},
    }
    fake_agent._runtime_tool_gateway_enabled = (
        lambda: QwenPawAgent._runtime_tool_gateway_enabled(fake_agent)
    )
    fake_agent._register_coding_mode_tools = lambda *_, **__: None

    context = _build_runtime_tool_gateway_context(fake_agent._request_context)
    toolkit = QwenPawAgent._create_toolkit(fake_agent, effective_skills=[])

    assert "No Runtime tool ids are currently allowed" in context
    assert "runtime_tool_gateway" in toolkit.tools
    assert "get_current_time" not in toolkit.tools
    assert "read_file" not in toolkit.tools


def test_runtime_gateway_skips_native_memory_tools() -> None:
    def memory_search():
        return "memory"

    class FakeMemoryManager:
        def list_memory_tools(self):
            return [memory_search]

    class FakeToolkit:
        def __init__(self):
            self.registered: list[str] = []

        def register_tool_function(self, tool_fn, **_kwargs):
            self.registered.append(tool_fn.__name__)

    fake_agent = SimpleNamespace()
    fake_agent._request_context = {
        "runtime_tool_gateway": {
            "base_url": "http://127.0.0.1:8765",
            "endpoint": "/runtime/v1/tool-calls",
            "task_id": "task_001",
            "allowed_tools": ["workspace.list_outputs"],
        }
    }
    fake_agent._runtime_tool_gateway_enabled = (
        lambda: QwenPawAgent._runtime_tool_gateway_enabled(fake_agent)
    )
    fake_agent.memory_manager = FakeMemoryManager()
    fake_agent.toolkit = FakeToolkit()
    fake_agent._namesake_strategy = "skip"

    QwenPawAgent._register_memory_tools(fake_agent)

    assert fake_agent.toolkit.registered == []


def test_runtime_gateway_builds_prompt_without_native_memory_manager(monkeypatch) -> None:
    captured: dict[str, object] = {}
    memory_manager = object()

    def fake_build_system_prompt_from_working_dir(**kwargs):
        captured["memory_manager"] = kwargs.get("memory_manager")
        return "base prompt"

    monkeypatch.setattr(
        "qwenpaw.agents.react_agent.build_system_prompt_from_working_dir",
        fake_build_system_prompt_from_working_dir,
    )
    monkeypatch.setattr(
        "qwenpaw.agents.react_agent.build_multimodal_hint",
        lambda: "",
    )
    fake_agent = SimpleNamespace()
    fake_agent._request_context = {
        "runtime_tool_gateway": {
            "base_url": "http://127.0.0.1:8765",
            "endpoint": "/runtime/v1/tool-calls",
            "task_id": "task_001",
            "allowed_tools": ["workspace.list_outputs"],
        }
    }
    fake_agent._agent_config = SimpleNamespace(heartbeat=None)
    fake_agent._workspace_dir = None
    fake_agent._language = "zh"
    fake_agent._env_context = None
    fake_agent.memory_manager = memory_manager
    fake_agent._runtime_tool_gateway_enabled = (
        lambda: QwenPawAgent._runtime_tool_gateway_enabled(fake_agent)
    )

    prompt = QwenPawAgent._build_sys_prompt(fake_agent)

    assert captured["memory_manager"] is None
    assert "Runtime Tool Gateway is enabled" in prompt


@pytest.mark.asyncio
async def test_runtime_tool_gateway_posts_allowed_tool_with_skill_context(monkeypatch) -> None:
    captured: dict = {}

    def fake_post_json(url: str, token: str, payload: dict, timeout_seconds: float):
        captured["url"] = url
        captured["token"] = token
        captured["payload"] = payload
        captured["timeout_seconds"] = timeout_seconds
        return {
            "status": "success",
            "tool_call_id": "tool_call_001",
            "result": {"tool_id": "workspace.list_outputs", "output_count": 0},
        }

    monkeypatch.setattr(
        runtime_tool_gateway_module,
        "_post_runtime_tool_gateway",
        fake_post_json,
    )
    set_current_runtime_tool_gateway(
        {
            "base_url": "http://127.0.0.1:8765",
            "endpoint": "/runtime/v1/tool-calls",
            "token": "worker-session-token",
            "task_id": "task_001",
            "session_id": "sess_001",
            "trace_id": "trace_001",
            "policy_snapshot_id": "ps_001",
            "tool_session_id": "wts_001",
            "allowed_tools": ["workspace.list_outputs"],
            "allowed_skill_contexts": [
                {
                    "skill_code": "workspace_reader",
                    "skill_version": "1.0.0",
                    "skill_catalog_version": "skill_cat_v1",
                    "skill_binding_version": "bind_v1",
                    "worker_skill_id": "qwenpaw-reader-v1",
                    "required_capabilities": ["file.read"],
                }
            ],
        }
    )

    response = await runtime_tool_gateway(
        tool_id="workspace.list_outputs",
        input={"task_id": "task_001"},
        idempotency_key="idem-001",
    )

    assert captured["url"] == "http://127.0.0.1:8765/runtime/v1/tool-calls"
    assert captured["token"] == "worker-session-token"
    assert captured["payload"]["tool_id"] == "workspace.list_outputs"
    assert captured["payload"]["input"] == {"task_id": "task_001"}
    assert captured["payload"]["skill_code"] == "workspace_reader"
    assert captured["payload"]["skill_binding_version"] == "bind_v1"
    assert "workspace.list_outputs" in _text(response)
    assert "worker-session-token" not in _text(response)


@pytest.mark.asyncio
async def test_runtime_tool_gateway_parses_json_string_input(monkeypatch) -> None:
    captured: dict = {}

    def fake_post_json(url: str, token: str, payload: dict, timeout_seconds: float):
        captured["payload"] = payload
        return {
            "status": "success",
            "tool_call_id": "tool_call_001",
            "result": {"tool_id": "file.parse_document", "file_id": "file_001"},
        }

    monkeypatch.setattr(
        runtime_tool_gateway_module,
        "_post_runtime_tool_gateway",
        fake_post_json,
    )
    set_current_runtime_tool_gateway(
        {
            "base_url": "http://127.0.0.1:8765",
            "endpoint": "/runtime/v1/tool-calls",
            "token": "worker-session-token",
            "task_id": "task_001",
            "session_id": "sess_001",
            "policy_snapshot_id": "ps_001",
            "tool_session_id": "wts_001",
            "allowed_tools": ["file.parse_document"],
        }
    )

    response = await runtime_tool_gateway(
        tool_id="file.parse_document",
        input='{"file_id":"file_001"}',
    )

    assert captured["payload"]["input"] == {"file_id": "file_001"}
    assert "file.parse_document" in _text(response)


@pytest.mark.asyncio
async def test_runtime_tool_gateway_rejects_unlisted_tool(monkeypatch) -> None:
    called = False

    def fake_post_json(*_args, **_kwargs):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(
        runtime_tool_gateway_module,
        "_post_runtime_tool_gateway",
        fake_post_json,
    )
    set_current_runtime_tool_gateway(
        {
            "base_url": "http://127.0.0.1:8765",
            "endpoint": "/runtime/v1/tool-calls",
            "token": "worker-session-token",
            "task_id": "task_001",
            "session_id": "sess_001",
            "policy_snapshot_id": "ps_001",
            "tool_session_id": "wts_001",
            "allowed_tools": ["workspace.list_outputs"],
        }
    )

    response = await runtime_tool_gateway(tool_id="execute_shell_command", input={})

    assert called is False
    assert "not allowed" in _text(response)
