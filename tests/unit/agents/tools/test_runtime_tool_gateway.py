# -*- coding: utf-8 -*-
"""Tests for the Runtime Tool Gateway bridge used by bank-runtime calls."""
from __future__ import annotations

import importlib

import pytest

from qwenpaw.config.context import set_current_runtime_tool_gateway
from qwenpaw.agents.react_agent import (
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


def test_runtime_disabled_tools_from_context_filters_native_tools() -> None:
    disabled = _runtime_disabled_tools_from_context(
        {
            "runtime_constraints": {
                "disabled_tools": ["execute_shell_command", "write_file"],
            }
        }
    )

    assert disabled == {"execute_shell_command", "write_file"}


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
