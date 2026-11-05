"""Presentation guidance reaches the SDK without rewriting requests or gating output."""
from copy import deepcopy
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from agentscope.formatter import OpenAIChatFormatter
from agentscope.message import AssistantMsg, SystemMsg, ToolCallBlock, UserMsg
from qwenpaw.providers.chat_message_order import normalize_system_messages

from bank_runtime.model_context import (
    ARTIFACT_EVIDENCE_GUIDANCE,
    OUTPUT_SCOPE_GUIDANCE,
    prepare_public_model_context,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("prompt", [
    "把这段改成一句话，只输出正文。",
    "提供三个版本，逐项解释区别。",
    "详细说明方案的前提、步骤和限制。",
    "解释这句话中的Word和Excel，不需要文件。",
])
async def test_output_scope_reaches_wire_preserving_user_and_personal_preferences(prompt):
    profile = SystemMsg("system", "个人偏好：使用中文，未指定时给出详细解释。")
    request = {"messages": [profile, UserMsg("user", prompt)], "tools": []}
    original = deepcopy(request)
    prepared = prepare_public_model_context(request)
    # A second preparation replaces the owned layer instead of accumulating rules.
    prepared = prepare_public_model_context(prepared)
    wire = normalize_system_messages(await OpenAIChatFormatter().format(prepared["messages"]))
    def wire_text(message):
        content = message["content"]
        return content if isinstance(content, str) else "\n".join(
            block["text"] for block in content if block.get("type") == "text"
        )

    system = "\n".join(wire_text(m) for m in wire if m["role"] == "system")
    assert system.count(OUTPUT_SCOPE_GUIDANCE) == 1
    assert system.rstrip().endswith(OUTPUT_SCOPE_GUIDANCE.rstrip())
    assert ARTIFACT_EVIDENCE_GUIDANCE not in system
    assert profile.get_text_content() in system
    assert [wire_text(m) for m in wire if m["role"] == "user"] == [prompt]
    assert request == original
    assert prepared["tools"] == []


@pytest.mark.parametrize("mode", ["delivery", "operation", "internal_read", "historical"])
def test_artifact_evidence_guidance_is_scoped_to_current_delivery(mode):
    operation = AssistantMsg("assistant", [ToolCallBlock(
        id="artifact-op", name="artifact_convert" if mode == "internal_read" else "artifact_generate",
        input='{"purpose":"read"}' if mode == "internal_read" else '{"artifact_type":"xlsx"}',
    )])
    messages = [UserMsg("user", "处理这份材料"), operation]
    if mode == "historical":
        messages.append(UserMsg("user", "只解释刚才的数字，不需要文件。"))
    request = {"messages": messages, "tools": [], "tool_choice": "auto"}
    original = deepcopy(request)
    prepared = prepare_public_model_context(request, delivery_required=mode == "delivery")
    reminder = prepared["messages"][-1].get_text_content()
    assert (ARTIFACT_EVIDENCE_GUIDANCE in reminder) is (mode in {"delivery", "operation"})
    assert request == original
    assert prepared["tool_choice"] == "auto"
    assert prepared["tools"] == []
