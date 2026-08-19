from __future__ import annotations

import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from bank_runtime.sandbox.scope import SandboxRequestScope
from bank_runtime.sandbox.tools import (
    SandboxToolState,
    reset_sandbox_tool_state,
    runtime_sandbox_files_search,
    runtime_sandbox_files_select,
    set_sandbox_tool_state,
)


class _Broker:
    async def search(self, *_args, **_kwargs):
        return [
            {
                "file_id": "file_history",
                "display_name": "历史材料.pdf",
                "content_type": "application/pdf",
                "size_bytes": 10,
                "source": "conversation",
                "readable": True,
                "object_key": "private/secret.pdf",
                "bucket": "private-bucket",
                "token": "secret",
            }
        ]


class _Cache:
    def __init__(self):
        self.calls = []

    async def prepare_files(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return []


class _Processor:
    def process(self, _prepared):
        return []


def _state():
    request = SimpleNamespace(
        runtime_task_id="task_001",
        sandbox_context={
            "context_id": "ctx_001",
            "task_id": "task_001",
            "signature": "signed",
        },
        attachments_manifest=[],
    )
    return SandboxToolState(
        scope=SandboxRequestScope.from_request(request),
        broker=_Broker(),
        cache=_Cache(),
        processor=_Processor(),
    )


@pytest.mark.asyncio
async def test_search_exposes_only_public_metadata_and_select_rejects_forgery() -> None:
    state = _state()
    token = set_sandbox_tool_state(state)
    try:
        search = await runtime_sandbox_files_search(query="历史")
        payload = json.loads(search.content[0].text)
        rendered = json.dumps(payload, ensure_ascii=False)
        assert payload["files"][0]["file_id"] == "file_history"
        assert "private/secret.pdf" not in rendered
        assert "private-bucket" not in rendered
        assert "secret" not in rendered

        forged = await runtime_sandbox_files_select(["file_forged"])
        assert "failed" in forged.content[0].text.lower()
        assert state.cache.calls == []
    finally:
        reset_sandbox_tool_state(token)
