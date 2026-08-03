# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import importlib

import pytest

from qwenpaw.config.context import set_current_runtime_sandbox_context


def _text(response) -> str:
    return "".join(
        str(part.get("text", "")) if isinstance(part, dict) else str(getattr(part, "text", ""))
        for part in response.content
    )


@pytest.mark.asyncio
async def test_context_expand_uses_only_current_signed_sandbox(monkeypatch) -> None:
    module = importlib.import_module(
        "qwenpaw.agents.tools.conversation_context_expand",
    )

    captured = {}

    class Client:
        def expand_conversation_context(self, before_turn_id, limit, sandbox_context):
            captured.update(
                before_turn_id=before_turn_id,
                limit=limit,
                sandbox_context=sandbox_context,
            )
            return {
                "messages": [
                    {
                        "role": "user",
                        "content": [{"type": "text", "text": "earlier"}],
                    }
                ],
                "has_more": False,
                "next_before_turn_id": "",
            }

    monkeypatch.setattr(module, "SandboxedOssClient", Client)
    sandbox = {"task_id": "task-a", "signature": "signed"}
    set_current_runtime_sandbox_context(sandbox)

    response = await module.conversation_context_expand("turn_boundary", 100)
    payload = json.loads(_text(response))

    assert captured == {
        "before_turn_id": "turn_boundary",
        "limit": 32,
        "sandbox_context": sandbox,
    }
    assert payload["messages"][0]["content"][0]["text"] == "earlier"
    assert "conversation_id" not in _text(response)


@pytest.mark.asyncio
async def test_context_expand_rejects_invalid_boundary_without_network(monkeypatch) -> None:
    module = importlib.import_module(
        "qwenpaw.agents.tools.conversation_context_expand",
    )

    class UnexpectedClient:
        def expand_conversation_context(self, *_args):
            raise AssertionError("network must not be called")

    monkeypatch.setattr(module, "SandboxedOssClient", UnexpectedClient)
    set_current_runtime_sandbox_context({"task_id": "task-a", "signature": "signed"})

    response = await module.conversation_context_expand("../other-session")

    assert "failed" in _text(response).lower()
