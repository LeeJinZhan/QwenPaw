from __future__ import annotations

import inspect
from pathlib import Path
import sys

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from bank_runtime.artifact_tools import (
    artifact_generate,
    artifact_revise,
    template_fill_docx,
)


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
    for tool in (artifact_generate, artifact_revise, template_fill_docx):
        assert not (forbidden & set(inspect.signature(tool).parameters))


def test_skill_requires_structured_runtime_tools_and_no_shell_fallback() -> None:
    skill = (PLUGIN_ROOT / "skills" / "bank-assistant-zh" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    for tool_name in ("artifact_generate", "artifact_revise", "template_fill_docx"):
        assert tool_name in skill
    assert "不得改用 shell" in skill
