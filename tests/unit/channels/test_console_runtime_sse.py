# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from agentscope_runtime.engine.schemas.agent_schemas import RunStatus

from qwenpaw.app.channels.console.channel import ConsoleChannel


class _FakeEvent:
    def __init__(self, payload: dict) -> None:
        self._payload = dict(payload)
        self.object = payload.get("object")
        self.status = payload.get("status")
        self.type = payload.get("type")
        self.output = payload.get("output")
        self.usage = payload.get("usage")

    def model_dump_json(self) -> str:
        return json.dumps(self._payload, ensure_ascii=False)


@pytest.mark.asyncio
async def test_console_stream_adds_runtime_completed_event_when_process_finishes() -> None:
    channel = ConsoleChannel(
        process=lambda _request: None,
        enabled=True,
        bot_prefix="",
    )

    async def fake_process(_request):
        yield _FakeEvent(
            {
                "object": "response",
                "status": RunStatus.InProgress,
                "type": "response",
                "output": None,
            }
        )
        yield _FakeEvent(
            {
                "object": "message",
                "status": RunStatus.InProgress,
                "type": "plugin_call",
                "content": None,
            }
        )

    channel._process = fake_process  # type: ignore[method-assign]
    request = SimpleNamespace(
        channel="bank-runtime",
        session_id="runtime-session-001",
        user_id="u001",
        channel_meta={"runtime_task_id": "task-runtime-001"},
        input=[],
    )

    events = [event async for event in channel.stream_one(request)]
    payloads = [
        json.loads(event.removeprefix("data:").strip())
        for event in events
        if event.startswith("data:")
    ]

    assert payloads[-1]["event"] == "completed"
    assert payloads[-1]["chat_id"] == "runtime-session-001"
