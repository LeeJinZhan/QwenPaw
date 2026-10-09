from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1] / "skills"


def test_complex_report_methods_are_readable_without_inventing_tabular_columns():
    import asyncio
    content = asyncio.run(_read_native_skill('bank-document-qa'))
    assert '展示标题、标签和值不等于统计列名' in content
    assert 'cross_sheet_union' in content and '所需真实列可对应' in content
    assert '固定单元格或汇总区' in content
    assert '文件引用由系统取得' in content
    assert '不能要求用户提供工具返回值或内部参数' in content


def test_general_and_writing_native_entries_defer_file_parameters():
    import asyncio
    import json

    for name in ('bank-assistant-zh', 'bank-document-writing'):
        content = asyncio.run(_read_native_skill(name))
        assert '```json' not in content
        assert 'bank-official-docx-v1' not in content
        assert 'bank-file-delivery' in content
    delivery = asyncio.run(_read_native_skill('bank-file-delivery'))
    examples = [json.loads(block) for block in re.findall(r'```json\s*\n(.*?)\n```', delivery, re.S)]
    assert {'docx', 'xlsx', 'pdf'} <= {x.get('artifact_type') for x in examples}
    official = next(x for x in examples if x.get('content', {}).get('kind') == 'official_document')
    assert official['content']['layout_version'] == 'bank-official-docx-v1'
    assert {'title', 'recipients', 'blocks'} <= official['content']['document'].keys()
    assert 'template_fill_docx' in delivery and '不得改用 shell' in delivery


def test_presentation_text_guidance_uses_layout_capacity_not_short_character_caps():
    content = (ROOT / "bank-presentation/SKILL.md").read_text()
    for obsolete in ("conclusion（100 字以内）", "最多 36 字", "不超过 16 字", "单元格不超过 2000 字"):
        assert obsolete not in content
    assert "完整说明页" in content and "资源预算" in content


def test_three_document_skills_are_packaged_with_valid_local_references():
    for name in ("bank-document-writing", "bank-document-review", "bank-document-qa", "bank-file-delivery"):
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
        ROOT / "bank-file-delivery/SKILL.md"
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


def test_real_native_viewer_returns_writing_and_delivery_rules_by_skill_name():
    import asyncio

    writing = asyncio.run(_read_native_skill("bank-document-writing"))
    assert "用户指定的篇幅" in writing and "bank-file-delivery" in writing
    delivery = asyncio.run(_read_native_skill("bank-file-delivery"))
    assert "bank-official-docx-v1" in delivery and '"blocks"' in delivery
    assert "template_fill_docx" in delivery
    assert "在已确定的公文 DOCX 任务中" in delivery
    for name in ("bank-document-review", "bank-document-qa"):
        assert "材料" in asyncio.run(_read_native_skill(name))
    general = asyncio.run(_read_native_skill("bank-assistant-zh"))
    assert "bank-official-docx-v1" not in general
    assert '"slides"' not in general
    assert '"sheets"' not in general and '"sheets"' in delivery
    qa = asyncio.run(_read_native_skill("bank-document-qa"))
    assert "conversion_report" in qa and "图形语义未核验" in qa


def test_table_completeness_rules_survive_native_skill_viewer():
    import asyncio

    content = asyncio.run(_read_native_skill("bank-document-qa"))
    for required in ("next_cursor", "has_more=false", "chunk_count", "按 chunk index 去重", "去重", "未分类项", "生成报告文件也不能替代完整性核对"):
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
    assert "retryable=false" in content and "精确页数" in content
    assert "不是仅生成了文字大纲" in content


def test_office_methods_and_shared_guidance_are_available_without_duplicate_rule_copies():
    import asyncio
    from agentscope.message import SystemMsg, UserMsg
    from bank_runtime.model_context import prepare_public_model_context
    from bank_runtime.presentation import PUBLIC_RESPONSE_GUIDANCE

    for name in ('bank-assistant-zh', 'bank-document-writing', 'bank-document-review', 'bank-document-qa', 'bank-presentation', 'bank-chart', 'bank-file-delivery'):
        content = asyncio.run(_read_native_skill(name))
        request = {'messages': [SystemMsg('system', PUBLIC_RESPONSE_GUIDANCE),
                                UserMsg('user', '按已确认要求继续处理')], 'tools': []}
        prepared = prepare_public_model_context(request)
        context = '\n'.join(message.get_text_content() for message in prepared['messages'])
        assert context.count(PUBLIC_RESPONSE_GUIDANCE) == 1
        assert '沿用仍有效的用户授权' in context
        assert '结果未知' in context
        assert '同一确定参数错误最多修正重试一次' not in content
        assert '用户未给出具体内容时，使用安全的 `cover`' not in content
        assert '## 交互与执行节奏' not in content


def test_official_document_maintenance_reference_matches_native_delivery_rules():
    reference = (ROOT / 'bank-document-writing/references/official-document-export.md').read_text()
    links = re.findall(r'\]\(([^)]+)\)', reference)
    assert len(links) == 1
    target = (ROOT / 'bank-document-writing/references' / links[0].split('#')[0]).resolve()
    assert target == (ROOT / 'bank-file-delivery/SKILL.md').resolve()
    assert '## 公文 DOCX 交付' in target.read_text()
    assert '```json' not in reference  # One maintained source for the contract.


def test_office_examples_keep_maintenance_instructions_out_of_artifact_content():
    import json

    for name in ('bank-assistant-zh', 'bank-document-writing', 'bank-presentation', 'bank-file-delivery'):
        content = (ROOT / name / 'SKILL.md').read_text()
        for block in re.findall(r'```json\s*\n(.*?)\n```', content, re.S):
            request = json.loads(block)
            artifact = json.dumps(request.get('content', {}), ensure_ascii=False)
            for instruction in ('本段仅为结构示例', '实际交付使用用户确认', '示例用于说明字段组合', '按用户已确认的安排更新本段'):
                assert instruction not in artifact, (name, instruction)


def test_native_office_skills_end_with_conditional_public_delivery_guidance():
    import asyncio

    for name in ('bank-assistant-zh', 'bank-document-writing', 'bank-document-review', 'bank-document-qa', 'bank-presentation', 'bank-file-delivery'):
        content = asyncio.run(_read_native_skill(name))
        delivery = content.split('## 面向用户的交付', 1)[1]
        assert '```json' not in delivery
        assert '文件' in delivery
        assert '未' in delivery


def test_native_writing_scope_covers_arbitrary_length_and_document_type():
    import asyncio
    content = asyncio.run(_read_native_skill('bank-document-writing'))
    assert '用户指定的篇幅' in content and '15000' not in content
    assert '关键缺项使任务无法继续' in content
    delivery = asyncio.run(_read_native_skill('bank-file-delivery'))
    assert '按实际文种选择 document_type' in delivery
    assert '例如【待填写】或【待核实】' in delivery
    assert '该文件的请示初稿' not in content
    assert '不使用×××占位' not in content
    before_docx = content.split('## 已确定 DOCX 交付时的文种与版式')[0]
    assert '先区分“写什么”和“交付什么”' in before_docx


def test_native_presentation_repairs_fields_not_incident_words_and_allows_labelled_examples():
    import asyncio
    content = asyncio.run(_read_native_skill('bank-presentation'))
    assert '已登记字段值与合法正文' in content
    assert '普通正文中的编程词汇如 for' not in content
    assert 'book、shield、chart 等已登记 icon' not in content
    assert '按任务需要使用明确标注的示例数据' in content
    assert 'retryable=false' in content and '精确页数' in content


def test_native_chart_generation_scope_is_not_editor_browsing_depth():
    import asyncio
    content = asyncio.run(_read_native_skill('bank-chart'))
    assert '关系范围按用户要求与实际取得材料确定' in content
    assert '上下各三层' not in content
    assert '用户明确指定导出格式时按该格式交付' in content
    assert 'schema_version="chart/1"' in content and 'source_refs' in content
