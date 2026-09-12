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


def test_table_completeness_rules_survive_native_skill_viewer():
    import asyncio

    content = asyncio.run(_read_native_skill("bank-document-qa"))
    for required in ("next_cursor", "has_more=false", "chunk_count", "按 chunk index 去重", "日均活跃用户数", "未分类项", "生成报告文件也不能替代完整性核对"):
        assert required in content


def test_presentation_skill_is_readable_via_native_skill_viewer():
    import asyncio
    content = asyncio.run(_read_native_skill("bank-presentation"))
    assert "artifact_generate" in content and "artifact_revise" in content
    assert "source_index" in content and "不授予" in content
    assert "bank-presentation" in (ROOT / "bank-assistant-zh/SKILL.md").read_text()
    for theme in ("steady_business", "modern_operations", "inclusive_local", "customer_value", "wealth_elegance", "digital_technology", "clear_classroom", "red_culture"):
        assert theme in content


def test_presentation_image_policy_guidance_is_available_via_native_skill_viewer():
    import asyncio

    content = asyncio.run(_read_native_skill("bank-presentation"))
    for required in ("image_policy", "uploaded_only", "source_index", "内置素材", "不传素材路径", "不对上传图片做视觉语义识别"):
        assert required in content


def test_presentation_summary_and_business_icons_survive_native_skill_viewer():
    import asyncio
    import json

    content = asyncio.run(_read_native_skill("bank-presentation"))
    examples = [json.loads(block) for block in re.findall(r"```json\s*\n(.*?)\n```", content, re.S)]
    summary = next(item for item in examples if item.get("layout") == "chart_summary")
    assert summary["chart"]["series"][0]["values"] == [250, 450]
    assert "summary" not in summary
    for icon in ("factory", "supply_chain", "globe", "logistics", "digital", "community", "training", "wallet"):
        assert icon in content
    for rule in ("合计由渲染器", "重复累计时期", "材料不明确时使用普通 chart", "不传 summary 字段", "无需自定义坐标"):
        assert rule in content


def test_presentation_complete_request_and_recovery_rules_survive_native_viewer():
    import asyncio
    import json

    content = asyncio.run(_read_native_skill("bank-presentation"))
    full = [json.loads(block) for block in re.findall(r"```json\s*\n(.*?)\n```", content, re.S)]
    assert any(item.get("artifact_type") == "pptx" and "content" in item for item in full)
    assert "上传 PPT 的文件编号不是" in content
    assert "清晰确定的参数错误可作一次有实质修改的重试" in content
    assert "不是仅生成了文字大纲" in content


def test_office_interaction_rules_are_available_when_each_skill_is_loaded_alone():
    import asyncio

    for name in ('bank-assistant-zh', 'bank-document-writing', 'bank-document-review', 'bank-document-qa', 'bank-presentation'):
        content = asyncio.run(_read_native_skill(name))
        for rule in ('不要重复询问', '只有关键缺项才集中询问', '不播报字段校验', '结果未知时不重复提交'):
            assert rule in content, (name, rule)
        assert '同一确定参数错误最多修正重试一次' in content


def test_official_document_maintenance_reference_matches_native_delivery_rules():
    main = (ROOT / 'bank-document-writing/SKILL.md').read_text()
    reference = (ROOT / 'bank-document-writing/references/official-document-export.md').read_text()
    assert reference.split('## 公文 DOCX 交付', 1)[1] == main.split('## 公文 DOCX 交付', 1)[1]


def test_office_examples_keep_maintenance_instructions_out_of_artifact_content():
    import json

    for name in ('bank-assistant-zh', 'bank-document-writing', 'bank-presentation'):
        content = (ROOT / name / 'SKILL.md').read_text()
        for block in re.findall(r'```json\s*\n(.*?)\n```', content, re.S):
            request = json.loads(block)
            artifact = json.dumps(request.get('content', {}), ensure_ascii=False)
            for instruction in ('本段仅为结构示例', '实际交付使用用户确认', '示例用于说明字段组合', '按用户已确认的安排更新本段'):
                assert instruction not in artifact, (name, instruction)


def test_native_office_skills_end_with_conditional_public_delivery_guidance():
    import asyncio

    for name in ('bank-assistant-zh', 'bank-document-writing', 'bank-document-review', 'bank-document-qa', 'bank-presentation'):
        content = asyncio.run(_read_native_skill(name))
        delivery = content.split('## 面向用户的交付', 1)[1]
        assert '```json' not in delivery
        assert '文件' in delivery
        assert '未' in delivery
        assert '技术原因' in content
