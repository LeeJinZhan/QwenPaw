# -*- coding: utf-8 -*-
"""Tests for the built-in bank assistant skill manifest."""

from pathlib import Path

import frontmatter


def test_bank_assistant_skill_manifest_guides_bank_tool_usage():
    skill_path = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "qwenpaw"
        / "agents"
        / "skills"
        / "bank_assistant-zh"
        / "SKILL.md"
    )

    post = frontmatter.loads(skill_path.read_text(encoding="utf-8"))

    assert post["name"] == "bank_assistant"
    assert "银行" in post["description"]
    assert "bank_assistant" in post.content
    assert "不要调用 shell" in post.content
    assert "客户号" in post.content
