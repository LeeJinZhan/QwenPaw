from __future__ import annotations

import inspect
from pathlib import Path
import sys

import pytest
from agentscope.message import TextBlock, ThinkingBlock, ToolCallBlock
from agentscope.model import ChatResponse


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from bank_runtime.artifact_tools import (
    ARTIFACT_RUNTIME_ACTION_BY_TOOL,
    ArtifactDeliveryIntent,
    ArtifactToolNotInvokedError,
    artifact_convert,
    artifact_generate,
    artifact_revise,
    parse_artifact_delivery_intent,
    template_fill_docx,
)
from bank_runtime.gateway.middleware import BankRuntimeGatewayMiddleware


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "kwargs"),
    [
        (
            artifact_generate,
            {"artifact_type": "docx", "title": "纪要", "content": {}},
        ),
        (
            artifact_revise,
            {
                "source_generated_file_id": "generated_file_001",
                "instructions": "补充结论",
                "content": {},
            },
        ),
        (
            artifact_convert,
            {
                "source_generated_file_id": "generated_file_001",
                "target_format": "pdf",
            },
        ),
        (
            template_fill_docx,
            {
                "template_version_id": "template_version_001",
                "title": "通知",
                "fields": {"title": "通知"},
            },
        ),
    ],
)
async def test_artifact_tool_functions_fail_closed_without_gateway(tool, kwargs):
    assert "必须通过 Bank Runtime Tool Gateway" in await tool(**kwargs)


def test_artifact_tool_signatures_expose_no_execution_authority() -> None:
    forbidden = {"command", "object_key", "path", "script", "shell", "url"}
    for tool in (
        artifact_generate,
        artifact_revise,
        artifact_convert,
        template_fill_docx,
    ):
        assert not (forbidden & set(inspect.signature(tool).parameters))


def test_artifact_delivery_intent_requires_structured_runtime_marker() -> None:
    intent = parse_artifact_delivery_intent(
        {
            "schema_version": "1.0",
            "kind": "artifact",
            "required": True,
            "operation": "convert",
            "target_format": "pdf",
            "source_refs": ["generated_file_001"],
        }
    )

    assert intent == ArtifactDeliveryIntent(
        operation="convert",
        target_format="pdf",
        source_refs=("generated_file_001",),
    )
    assert parse_artifact_delivery_intent("请生成一个 DOCX") is None
    assert parse_artifact_delivery_intent(
        {
            "schema_version": "1.0",
            "kind": "artifact",
            "required": False,
            "operation": "generate",
            "target_format": "docx",
        }
    ) is None
    assert parse_artifact_delivery_intent(
        {
            "schema_version": "1.0",
            "kind": "artifact",
            "required": True,
            "operation": "chat",
            "target_format": "docx",
        }
    ) is None


async def _run_model_call_in_reply(
    middleware: BankRuntimeGatewayMiddleware,
    responses: list[object],
    *,
    tools: list[dict] | None = None,
) -> tuple[list[ChatResponse], list[dict]]:
    calls: list[dict] = []

    async def model_call(**kwargs):
        calls.append(kwargs)
        return responses.pop(0)

    async def reply(**_kwargs):
        response = await middleware.on_model_call(
            agent=object(),
            input_kwargs={
                "messages": [],
                "tools": tools
                or [
                    {"type": "function", "function": {"name": name}}
                    for name in ARTIFACT_RUNTIME_ACTION_BY_TOOL
                ],
                "tool_choice": None,
                "current_model": object(),
            },
            next_handler=model_call,
        )
        yield response

    output = [
        item
        async for item in middleware.on_reply(
            object(),
            {"inputs": None},
            reply,
        )
    ]
    return output, calls


async def _stream_response(*chunks: ChatResponse):
    for chunk in chunks:
        yield chunk


@pytest.mark.asyncio
async def test_explicit_artifact_delivery_replans_once_and_discards_fake_text() -> None:
    middleware = BankRuntimeGatewayMiddleware(
        None,
        artifact_intent=ArtifactDeliveryIntent(
            operation="generate",
            target_format="docx",
        ),
    )
    draft = ChatResponse(
        content=[
            ThinkingBlock(thinking="raw private reasoning"),
            TextBlock(text="已生成 fake.docx，请下载。"),
        ],
        is_last=True,
    )
    tool_call = ChatResponse(
        content=[
            ToolCallBlock(
                id="call-1",
                name="artifact_generate",
                input='{"artifact_type":"docx"}',
            )
        ],
        is_last=True,
    )

    output, calls = await _run_model_call_in_reply(
        middleware,
        [draft, tool_call],
    )

    assert output == [tool_call]
    assert len(calls) == 2
    assert calls[1]["tool_choice"].mode == "required"
    assert set(calls[1]["tool_choice"].tools or []) == set(
        ARTIFACT_RUNTIME_ACTION_BY_TOOL
    )
    assert all(
        schema["function"]["name"] in ARTIFACT_RUNTIME_ACTION_BY_TOOL
        for schema in calls[1]["tools"]
    )


@pytest.mark.asyncio
async def test_explicit_artifact_delivery_supports_streaming_model_output() -> None:
    middleware = BankRuntimeGatewayMiddleware(
        None,
        artifact_intent=ArtifactDeliveryIntent(
            operation="generate",
            target_format="pptx",
        ),
    )
    draft = _stream_response(
        ChatResponse(
            content=[ThinkingBlock(thinking="private draft")],
            is_last=False,
        ),
        ChatResponse(content=[TextBlock(text="fake.pptx")], is_last=True),
    )
    tool_start = ChatResponse(
        content=[ThinkingBlock(thinking="selecting governed tool")],
        is_last=False,
    )
    tool_final = ChatResponse(
        content=[
            ToolCallBlock(
                id="call-stream",
                name="artifact_generate",
                input='{"artifact_type":"pptx"}',
            )
        ],
        is_last=True,
    )
    replacement = _stream_response(tool_start, tool_final)

    output, calls = await _run_model_call_in_reply(
        middleware,
        [draft, replacement],
    )
    replayed = output[0]
    assert hasattr(replayed, "__aiter__")
    assert [chunk async for chunk in replayed] == [tool_start, tool_final]
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_second_zero_artifact_call_returns_stable_error() -> None:
    middleware = BankRuntimeGatewayMiddleware(
        None,
        artifact_intent=ArtifactDeliveryIntent(
            operation="generate",
            target_format="xlsx",
        ),
    )
    responses = [
        ChatResponse(content=[TextBlock(text="先写脚本")], is_last=True),
        ChatResponse(content=[TextBlock(text="已生成 result.xlsx")], is_last=True),
    ]

    with pytest.raises(ArtifactToolNotInvokedError) as exc_info:
        await _run_model_call_in_reply(middleware, responses)

    assert exc_info.value.error_code == "ARTIFACT_TOOL_NOT_INVOKED"


@pytest.mark.asyncio
async def test_ordinary_question_is_not_forced_to_call_artifact_tool() -> None:
    middleware = BankRuntimeGatewayMiddleware(None, artifact_intent=None)
    answer = ChatResponse(content=[TextBlock(text="普通回答")], is_last=True)

    output, calls = await _run_model_call_in_reply(middleware, [answer])

    assert output == [answer]
    assert len(calls) == 1


def test_skill_requires_structured_runtime_tools_and_no_shell_fallback() -> None:
    skill = (PLUGIN_ROOT / "skills" / "bank-assistant-zh" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    for tool_name in (
        "artifact_generate",
        "artifact_revise",
        "artifact_convert",
        "template_fill_docx",
    ):
        assert tool_name in skill
    assert "不得改用 shell" in skill


def test_request_security_overlay_makes_office_tool_choice_mandatory() -> None:
    personalization = (PLUGIN_ROOT / "bank_runtime" / "personalization.py").read_text(
        encoding="utf-8"
    )

    assert "MUST call artifact_generate" in personalization
    assert "Never create an Office deliverable as a Python, Node, shell, or macro script" in personalization
