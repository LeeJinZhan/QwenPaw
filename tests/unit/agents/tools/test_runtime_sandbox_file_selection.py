# -*- coding: utf-8 -*-
"""Worker-owned supplemental file selection contract tests."""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from agentscope.message import Msg

from qwenpaw.config.context import (
    get_current_runtime_discovered_files,
    reset_current_runtime_discovered_files,
    reset_current_runtime_selected_file_ids,
    set_current_runtime_discovered_files,
    set_current_runtime_sandbox_context,
    set_current_runtime_selected_file_ids,
)


def test_file_search_normalizes_list_all_and_limited_extension_wildcards() -> None:
    module = importlib.import_module(
        "qwenpaw.agents.tools.runtime_sandbox_files",
    )

    assert module._normalize_search_filters("*", None) == ("", [])
    assert module._normalize_search_filters("*.PNG", None) == ("", [".png"])
    assert module._normalize_search_filters("客户*.png", None) == (
        "客户",
        [".png"],
    )
    assert module._normalize_search_filters("logo.png", ["png"]) == (
        "logo.png",
        [".png"],
    )


@pytest.mark.parametrize(
    "query",
    [
        "帮我看下第二份文件的内容",
        "看下截图文件",
        "看下文件区有哪些文件",
        "查看有哪些png格式的文件",
    ],
)
def test_supplemental_file_intent_recognizes_verb_first_and_ordinal_queries(
    query: str,
) -> None:
    react_agent = importlib.import_module("qwenpaw.agents.react_agent")

    assert react_agent._runtime_supplemental_file_reference_requested(query) is True


def test_supplemental_file_intent_extracts_structured_extension_filters() -> None:
    react_agent = importlib.import_module("qwenpaw.agents.react_agent")

    assert react_agent._runtime_supplemental_extensions(
        "查看有哪些 PNG 格式和 markdown 文件",
    ) == [".png", ".md"]


@pytest.mark.asyncio
async def test_extension_file_query_discovers_only_matching_metadata(monkeypatch) -> None:
    react_agent = importlib.import_module("qwenpaw.agents.react_agent")
    captured: dict[str, object] = {}

    class FakeClient:
        def search_files(
            self,
            query,
            content_types,
            sources,
            limit,
            sandbox_context,
            *,
            extensions=None,
        ):
            captured.update(
                query=query,
                content_types=content_types,
                sources=sources,
                limit=limit,
                sandbox_context=sandbox_context,
                extensions=extensions,
            )
            return {
                "files": [
                    {
                        "file_id": "file_png",
                        "display_name": "screen.png",
                        "content_type": "image/png",
                        "size_bytes": 32,
                        "source": "assistant_workspace",
                        "readable": True,
                    }
                ]
            }

    monkeypatch.setattr(react_agent.runtime_sandbox_oss, "SandboxedOssClient", FakeClient)
    discovered_token = set_current_runtime_discovered_files([])
    try:
        request = Msg("user", "查看有哪些png格式的文件", "user")
        await react_agent._append_runtime_auto_selected_content_parts(
            request,
            {"sandbox_context": {"task_id": "task_001", "context_id": "ctx_001"}},
        )
    finally:
        reset_current_runtime_discovered_files(discovered_token)

    assert captured["extensions"] == [".png"]
    assert captured["query"] == ""


@pytest.mark.asyncio
async def test_file_search_forwards_structured_extensions(monkeypatch) -> None:
    module = importlib.import_module(
        "qwenpaw.agents.tools.runtime_sandbox_files",
    )
    captured: dict[str, object] = {}

    class FakeClient:
        def search_files(
            self,
            query,
            content_types,
            sources,
            limit,
            sandbox_context,
            *,
            extensions=None,
        ):
            captured.update(
                query=query,
                content_types=content_types,
                extensions=extensions,
                sources=sources,
                limit=limit,
                sandbox_context=sandbox_context,
            )
            return {"files": []}

    monkeypatch.setattr(module, "SandboxedOssClient", FakeClient)
    sandbox_token = set_current_runtime_sandbox_context(
        {"task_id": "task_001", "context_id": "ctx_001"},
    )
    try:
        await module.runtime_sandbox_files_search(
            query="客户*.PNG",
            extensions=[".jpg"],
        )
    finally:
        set_current_runtime_sandbox_context(sandbox_token)

    assert captured == {
        "query": "客户",
        "content_types": [],
        "extensions": [".jpg", ".png"],
        "sources": ["conversation", "assistant_workspace"],
        "limit": 20,
        "sandbox_context": {"task_id": "task_001", "context_id": "ctx_001"},
    }


@pytest.mark.asyncio
async def test_select_rejects_file_id_not_returned_by_search(monkeypatch) -> None:
    module = importlib.import_module(
        "qwenpaw.agents.tools.runtime_sandbox_file_select",
    )
    prepared = False

    class UnexpectedCache:
        def prepare_files(self, *_args, **_kwargs):
            nonlocal prepared
            prepared = True
            raise AssertionError("undiscovered file must not be prepared")

    monkeypatch.setattr(
        module.runtime_sandbox_oss,
        "_DEFAULT_TASK_ATTACHMENT_CACHE",
        UnexpectedCache(),
    )
    set_current_runtime_sandbox_context(
        {"task_id": "task_001", "context_id": "ctx_001"},
    )
    discovered_token = set_current_runtime_discovered_files(
        [{"file_id": "file_allowed", "display_name": "allowed.md", "source": "conversation"}],
    )
    selected_token = set_current_runtime_selected_file_ids(frozenset())
    try:
        response = await module.runtime_sandbox_files_select(["file_forged"])
    finally:
        reset_current_runtime_selected_file_ids(selected_token)
        reset_current_runtime_discovered_files(discovered_token)

    assert prepared is False
    assert "could not be matched" in response.content[0]["text"]
    assert "Do not try another file-reading method" in response.content[0]["text"]
    assert "Runtime" not in response.content[0]["text"]
    assert "policy" not in response.content[0]["text"]


@pytest.mark.asyncio
async def test_select_refreshes_request_local_candidates_before_rejecting_stale_id(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module = importlib.import_module(
        "qwenpaw.agents.tools.runtime_sandbox_file_select",
    )
    runtime_sandbox_oss = importlib.import_module(
        "qwenpaw.agents.tools.runtime_sandbox_oss",
    )
    local_path = tmp_path / "runtime-readme.md"
    local_path.write_text("# Runtime", encoding="utf-8")
    prepared = runtime_sandbox_oss.PreparedSandboxFile(
        file_id="file_previous",
        local_path=local_path,
        content_type="text/markdown",
        size_bytes=local_path.stat().st_size,
        original_name="runtime-readme.md",
        expires_at="",
    )
    searches: list[tuple[str, list[str]]] = []

    class FakeClient:
        def search_files(self, query, _content_types, sources, _limit, _sandbox_context):
            searches.append((query, list(sources)))
            return {
                "files": [
                    {
                        "file_id": "file_previous",
                        "display_name": "runtime-readme.md",
                        "content_type": "text/markdown",
                        "size_bytes": local_path.stat().st_size,
                        "source": "assistant_workspace",
                        "readable": True,
                    }
                ]
            }

    class FakeCache:
        def prepare_files(self, file_ids, _sandbox_context, *, selection_records=None):
            assert file_ids == ["file_previous"]
            assert selection_records == [
                {
                    "file_id": "file_previous",
                    "source": "assistant_workspace",
                    "selection_mode": "model_metadata_selection",
                }
            ]
            return [prepared]

        def reserve_task_io(self, task_id, *, file_id=""):
            class Reservation:
                def release(self):
                    return None

            return Reservation()

    monkeypatch.setattr(module.runtime_sandbox_oss, "SandboxedOssClient", FakeClient)
    monkeypatch.setattr(
        module.runtime_sandbox_oss,
        "_DEFAULT_TASK_ATTACHMENT_CACHE",
        FakeCache(),
    )
    sandbox_token = set_current_runtime_sandbox_context(
        {"task_id": "task_001", "context_id": "ctx_001"},
    )
    discovered_token = set_current_runtime_discovered_files([])
    selected_token = set_current_runtime_selected_file_ids(frozenset())
    try:
        response = await module.runtime_sandbox_files_select(["file_previous"])
    finally:
        reset_current_runtime_selected_file_ids(selected_token)
        reset_current_runtime_discovered_files(discovered_token)
        set_current_runtime_sandbox_context(sandbox_token)

    assert searches == [("", ["conversation", "assistant_workspace"])]
    assert "Runtime" in response.content[1]["text"]


@pytest.mark.asyncio
async def test_select_prepares_discovered_files_and_returns_model_content(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module = importlib.import_module(
        "qwenpaw.agents.tools.runtime_sandbox_file_select",
    )
    runtime_sandbox_oss = importlib.import_module(
        "qwenpaw.agents.tools.runtime_sandbox_oss",
    )
    local_path = tmp_path / "policy.md"
    local_path.write_text("# Policy\n人工复核", encoding="utf-8")
    prepared = runtime_sandbox_oss.PreparedSandboxFile(
        file_id="file_policy",
        local_path=local_path,
        content_type="text/markdown",
        size_bytes=local_path.stat().st_size,
        original_name="policy.md",
        expires_at="",
    )
    captured: dict[str, object] = {}

    class FakeCache:
        def prepare_files(
            self,
            file_ids,
            sandbox_context,
            *,
            selection_records=None,
        ):
            captured["file_ids"] = list(file_ids)
            captured["sandbox_context"] = dict(sandbox_context)
            captured["selection_records"] = list(selection_records or [])
            return [prepared]

        def reserve_task_io(self, task_id, *, file_id=""):
            class Reservation:
                def release(self):
                    return None

            return Reservation()

    monkeypatch.setattr(
        module.runtime_sandbox_oss,
        "_DEFAULT_TASK_ATTACHMENT_CACHE",
        FakeCache(),
    )
    set_current_runtime_sandbox_context(
        {"task_id": "task_001", "context_id": "ctx_001"},
    )
    discovered_token = set_current_runtime_discovered_files(
        [{"file_id": "file_policy", "display_name": "policy.md", "source": "assistant_workspace"}],
    )
    selected_token = set_current_runtime_selected_file_ids(frozenset())
    try:
        response = await module.runtime_sandbox_files_select(["file_policy"])
    finally:
        reset_current_runtime_selected_file_ids(selected_token)
        reset_current_runtime_discovered_files(discovered_token)

    assert captured == {
        "file_ids": ["file_policy"],
        "sandbox_context": {"task_id": "task_001", "context_id": "ctx_001"},
        "selection_records": [
            {
                "file_id": "file_policy",
                "source": "assistant_workspace",
                "selection_mode": "model_metadata_selection",
            },
        ],
    }
    assert "assistant_workspace" in response.content[0]["text"]
    assert "人工复核" in response.content[1]["text"]


@pytest.mark.asyncio
async def test_explicit_unique_filename_is_auto_selected_but_greeting_is_not(
    monkeypatch,
    tmp_path: Path,
) -> None:
    react_agent = importlib.import_module("qwenpaw.agents.react_agent")
    searched: list[str] = []
    selected: list[str] = []

    class FakeClient:
        def search_files(self, query, content_types, sources, limit, sandbox_context):
            searched.append(query)
            return {
                "files": [
                    {
                        "file_id": "file_policy",
                        "display_name": "policy.md",
                        "content_type": "text/markdown",
                        "size_bytes": 16,
                        "source": "conversation",
                        "readable": True,
                    }
                ]
            }

    async def fake_append(msg, request_context, file_ids, **_kwargs):
        selected.extend(file_ids)
        return msg

    monkeypatch.setattr(react_agent.runtime_sandbox_oss, "SandboxedOssClient", FakeClient)
    monkeypatch.setattr(
        react_agent,
        "_append_selected_runtime_attachment_content_parts",
        fake_append,
    )
    context = {
        "sandbox_context": {"task_id": "task_001", "context_id": "ctx_001"},
        "attachments_manifest": [
            {"file_id": "file_current", "source": "current_task"},
        ],
    }

    greeting = Msg("user", "你好", "user")
    await react_agent._append_runtime_auto_selected_content_parts(greeting, context)
    assert searched == []
    assert selected == []

    current_attachment_request = Msg(
        "user",
        "识别一下这份文件内容并总结",
        "user",
    )
    # This represents the content parts appended from this task's Excel file.
    # Discovery must use the original user text, not infer a historical-file
    # lookup from text emitted by the attachment processor.
    current_attachment_request.content = [
        {"type": "text", "text": "识别一下这份文件内容并总结"},
        {"type": "text", "text": "[Spreadsheet: 8_5.xlsx]"},
    ]
    await react_agent._append_runtime_auto_selected_content_parts(
        current_attachment_request,
        context,
        user_text="识别一下这份文件内容并总结",
    )
    assert searched == []
    assert selected == []

    request = Msg("user", "请总结之前上传的 policy.md", "user")
    await react_agent._append_runtime_auto_selected_content_parts(request, context)
    assert searched == ["policy.md"]
    assert selected == ["file_policy"]


@pytest.mark.asyncio
async def test_explicit_filename_with_multiple_matches_is_not_auto_selected(
    monkeypatch,
) -> None:
    react_agent = importlib.import_module("qwenpaw.agents.react_agent")
    selected: list[str] = []

    class FakeClient:
        def search_files(self, query, content_types, sources, limit, sandbox_context):
            assert query == "policy.md"
            return {
                "files": [
                    {
                        "file_id": "file_policy_1",
                        "display_name": "policy.md",
                        "source": "conversation",
                        "readable": True,
                    },
                    {
                        "file_id": "file_policy_2",
                        "display_name": "policy.md",
                        "source": "assistant_workspace",
                        "readable": True,
                    },
                ],
            }

    async def fake_append(msg, request_context, file_ids, **_kwargs):
        selected.extend(file_ids)
        return msg

    monkeypatch.setattr(react_agent.runtime_sandbox_oss, "SandboxedOssClient", FakeClient)
    monkeypatch.setattr(
        react_agent,
        "_append_selected_runtime_attachment_content_parts",
        fake_append,
    )

    request = Msg("user", "请总结之前上传的 policy.md", "user")
    await react_agent._append_runtime_auto_selected_content_parts(
        request,
        {"sandbox_context": {"task_id": "task_001", "context_id": "ctx_001"}},
    )

    assert selected == []


@pytest.mark.asyncio
async def test_ambiguous_historical_file_reference_discovers_metadata_only(
    monkeypatch,
) -> None:
    react_agent = importlib.import_module("qwenpaw.agents.react_agent")
    searches: list[tuple[str, list[str], int]] = []

    class FakeClient:
        def search_files(self, query, _content_types, sources, limit, _sandbox_context):
            searches.append((query, list(sources), limit))
            return {
                "files": [
                    {
                        "file_id": "file_previous",
                        "display_name": "客户材料.pdf",
                        "content_type": "application/pdf",
                        "size_bytes": 128,
                        "source": "assistant_workspace",
                        "readable": True,
                    }
                ]
            }

    monkeypatch.setattr(react_agent.runtime_sandbox_oss, "SandboxedOssClient", FakeClient)
    discovered_token = set_current_runtime_discovered_files([])
    try:
        request = Msg("user", "请总结我之前上传的材料", "user")
        result = await react_agent._append_runtime_auto_selected_content_parts(
            request,
            {"sandbox_context": {"task_id": "task_001", "context_id": "ctx_001"}},
        )
        assert result is request
        assert searches == [("", ["conversation", "assistant_workspace"], 10)]
        assert get_current_runtime_discovered_files()["file_previous"]["source"] == (
            "assistant_workspace"
        )
        assert isinstance(request.content, list)
        assert "never quote or expose" in request.content[-1]["text"]
        assert "file_previous" in request.content[-1]["text"]
    finally:
        reset_current_runtime_discovered_files(discovered_token)


@pytest.mark.asyncio
async def test_assistant_file_area_search_does_not_include_conversation_files(
    monkeypatch,
) -> None:
    react_agent = importlib.import_module("qwenpaw.agents.react_agent")
    searches: list[tuple[str, list[str], int]] = []

    class FakeClient:
        def search_files(self, query, _content_types, sources, limit, _sandbox_context):
            searches.append((query, list(sources), limit))
            return {
                "files": [
                    {
                        "file_id": "file_assistant",
                        "display_name": "助手资料.md",
                        "content_type": "text/markdown",
                        "size_bytes": 128,
                        "source": "assistant_workspace",
                        "readable": True,
                    }
                ]
            }

    monkeypatch.setattr(react_agent.runtime_sandbox_oss, "SandboxedOssClient", FakeClient)
    discovered_token = set_current_runtime_discovered_files([])
    try:
        request = Msg("user", "助手文件区", "user")
        result = await react_agent._append_runtime_auto_selected_content_parts(
            request,
            {"sandbox_context": {"task_id": "task_001", "context_id": "ctx_001"}},
        )

        assert result is request
        assert searches == [("", ["assistant_workspace"], 10)]
        assert isinstance(request.content, list)
        metadata_context = request.content[-1]["text"]
        assert "助手资料.md" in metadata_context
        assert "never quote or expose" in metadata_context
        assert "runtime_sandbox_files_select" not in metadata_context
    finally:
        reset_current_runtime_discovered_files(discovered_token)


def test_auto_selection_is_bounded() -> None:
    module = importlib.import_module(
        "qwenpaw.agents.tools.runtime_sandbox_file_select",
    )

    assert module.MAX_RUNTIME_AUTONOMOUS_SELECTION_FILES == 3


def test_runtime_attachment_read_is_not_exported_as_model_tool() -> None:
    tools = importlib.import_module("qwenpaw.agents.tools")
    react_agent = importlib.import_module("qwenpaw.agents.react_agent")

    assert "runtime_attachment_read" not in tools.__all__
    assert not hasattr(react_agent, "runtime_attachment_read")
