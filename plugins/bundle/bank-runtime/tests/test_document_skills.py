from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1] / "skills"


def test_three_document_skills_are_packaged_with_valid_local_references():
    for name in ("bank-document-writing", "bank-document-review", "bank-document-qa"):
        entry = ROOT / name / "SKILL.md"
        assert entry.is_file()
        content = entry.read_text()
        assert f"name: {name}" in content
        for path in [entry, *(entry.parent / "references").glob("*.md")]:
            for link in re.findall(r"\]\(([^)]+)\)", path.read_text()):
                target = (path.parent / link.split("#")[0]).resolve()
                assert target.is_relative_to(ROOT.resolve()) and target.is_file(), (
                    path,
                    link,
                )
        assert "不授予" in content or "不赋予" in content


def test_bank_entry_keeps_identity_rules_and_routes_to_document_skills():
    content = (ROOT / "bank-assistant-zh/SKILL.md").read_text()
    assert "已认证的 Runtime 请求上下文" in content
    for name in ("bank-document-writing", "bank-document-review", "bank-document-qa"):
        assert name in content
    writing = (
        ROOT / "bank-document-writing/references/official-document-export.md"
    ).read_text()
    assert "artifact_generate" in writing and "artifact_revise" in writing
    assert "template_fill_docx" in writing
    assert "bank-official-docx-v1" in writing
    assert (
        "generate_official_document" not in writing
        and "send_file_to_user" not in writing
    )


async def _read_native_skill(name):
    import json
    from types import SimpleNamespace
    from agentscope.message import ToolCallBlock
    from agentscope.permission import PermissionBehavior
    from agentscope.skill import Skill
    from agentscope.state import AgentState
    from agentscope.tool import Toolkit
    from test_native_skill_boundary import DenyingClient, Guard
    from bank_runtime.gateway.middleware import (
        BankRuntimeGatewayMiddleware,
        GatewayPermissionEngine,
    )

    text = (ROOT / name / "SKILL.md").read_text()
    agent = SimpleNamespace(
        toolkit=Toolkit(
            tools=[],
            skills_or_loaders=[
                Skill(
                    name=name,
                    description="文档技能",
                    dir=str(ROOT / name),
                    markdown=text,
                    updated_at=0,
                )
            ],
        ),
        state=AgentState(),
    )
    middleware = BankRuntimeGatewayMiddleware(DenyingClient())
    middleware.bind_native_skills(agent)
    engine = GatewayPermissionEngine(Guard(), middleware)
    assert (
        await engine.check_permission(
            agent.toolkit.builtin_skill_viewer.tool, {"skill": name}
        )
    ).behavior == PermissionBehavior.ALLOW
    call = ToolCallBlock(
        id="read-document", name="Skill", input=json.dumps({"skill": name})
    )

    async def execute():
        async for item in agent.toolkit.call_tool(call, agent.state):
            yield item

    result = [
        item async for item in middleware.on_acting(agent, {"tool_call": call}, execute)
    ]
    return result[-1].content[0].text


def test_real_native_viewer_returns_complete_document_rules_without_reference_read_tool():
    import asyncio

    writing = asyncio.run(_read_native_skill("bank-document-writing"))
    assert "bank-official-docx-v1" in writing and '"blocks"' in writing
    assert "template_fill_docx" in writing and "15000" in writing
    for name in ("bank-document-review", "bank-document-qa"):
        assert "材料" in asyncio.run(_read_native_skill(name))
