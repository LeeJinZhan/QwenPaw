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

@pytest.mark.asyncio
async def test_converted_result_issues_parser_reference_after_authorization(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    import hashlib
    import io
    import zipfile
    from bank_runtime.sandbox.cache import TaskAttachmentCache
    from bank_runtime.sandbox.file_refs import FileRefRegistry
    from bank_runtime.sandbox.processor import AttachmentProcessor
    from bank_runtime.sandbox.tools import converted_attachment_blocks
    import bank_runtime.sandbox.tools as module
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr("word/document.xml", "<document>source text</document>")
    content = data.getvalue()
    class Broker:
        async def authorize_files(self, scope, ids, selection_records=None):
            assert ids == ["gfile_converted"] and scope.task_id == "task_001"
            return {"authorized": [{"file_id":ids[0],"original_name":"converted.docx","content_type":"application/vnd.openxmlformats-officedocument.wordprocessingml.document","size_bytes":len(content),"content_hash":hashlib.sha256(content).hexdigest(),"expires_at":"2099-01-01T00:00:00Z"}], "denied":[]}
        def stream_locator(self, locator, write_chunk):
            write_chunk(content)
    state = _state()
    state.scope.sandbox_context["expires_at"] = "2099-01-01T00:00:00Z"
    state.cache = TaskAttachmentCache(root=tmp_path)
    state.broker = Broker()
    state.processor = AttachmentProcessor()
    registry = FileRefRegistry(tmp_path)
    monkeypatch.setattr(module, "get_file_ref_registry", lambda: registry)
    token = set_sandbox_tool_state(state)
    try:
        blocks = await converted_attachment_blocks({"source_type":"session_file", "source_id":"file_old", "target_format":"docx"}, {"artifact_status":"succeeded", "generated_file_ids":["gfile_converted"]})
        import re
        ref = re.search(r'file_ref="([^"]+)"', blocks[0].text).group(1)
        resolved = registry.resolve(ref, expected_task_id="task_001")
        assert resolved.file_id == "gfile_converted"
        assert resolved.path.read_bytes() == content
        assert 'processing="tool_required"' in blocks[0].text
        assert str(tmp_path) not in blocks[0].text
    finally:
        registry.revoke_task(state.scope.task_id)
        await state.cache.cleanup(state.scope.task_id)
        reset_sandbox_tool_state(token)


@pytest.mark.asyncio
async def test_conversion_read_failure_is_logged_without_raw_details(caplog):
    from bank_runtime.sandbox.cache import SandboxCacheError
    from bank_runtime.sandbox.tools import converted_attachment_blocks

    class FailedCache:
        async def prepare_files(self, *args):
            raise SandboxCacheError("private-filename private-token")

    state = _state()
    state.cache = FailedCache()
    token = set_sandbox_tool_state(state)
    try:
        blocks = await converted_attachment_blocks(
            {"source_type": "session_file", "source_id": "file_old", "target_format": "docx"},
            {"artifact_status": "succeeded", "generated_file_ids": ["gfile_converted"]},
        )
        assert "正文尚未读取" in blocks[0].text
        assert "SandboxCacheError" in caplog.text
        assert "private-" not in caplog.text + blocks[0].text
    finally:
        reset_sandbox_tool_state(token)
