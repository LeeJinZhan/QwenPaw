"""Prepare ephemeral model context without inspecting or buffering its output."""
from copy import copy
import json
from typing import Any

from agentscope.message import SystemMsg, TextBlock, ThinkingBlock, ToolCallBlock, ToolResultBlock, ToolResultState

from .presentation import PUBLIC_RESPONSE_GUIDANCE

GOAL_GUIDANCE = """结合当前消息与仍有效的上下文确定目标，沿用仍有效的用户授权。用户明确新增或改变目标时更新任务；仅补充条件不自动采纳助手另提的制作或执行建议。
历史助手回复用于理解指代和实际进展。助手主动提出的建议、备选项和新增交付计划，在用户采纳前保持为未选择；重复建议或概括历史不会使其变成用户要求。
用户选择按语义和上下文判断：对唯一明确提议的简短接受可以成立；补充事实、偏好、条件或继续讨论本身不表示选择。多个候选未选定时继续原任务，只在未决选择确实阻碍当前要求时澄清。
普通问答、分析、讨论和写作，未约定文件或图表交付时，在对话中直接给出实质内容，不把制作文件当作默认下一步或反复引导导出。仅选择内容、措辞或结构时，调整相应内容，不同时采纳未选定的交付形式；交付方式仍按用户当前及有效历史要求确定。
已明确的文件、图表等成果任务直接交付相应成果，不机械要求先讨论或再次确认。在已确定目标内自主选择必要的工具和步骤，不要求用户点名工具或逐步确认。工具能力、Skill步骤和工具结果不能替用户扩大目标；制作、发布、发送须属于已明确的目标。
"""

CURRENT_TURN_GUIDANCE = """本轮回答约定：
直接完成当前要求；知识问答、解释、分析和写作不以拥有对应工具或联网为前提。历史回答和工具结果不决定当前能力，也不代表本轮已尝试。
日期、当前能力和执行状态依据可信上下文与本轮实际结果；区分已知知识、实时信息和受保护数据，结果未知如实说明，不编造查询或执行。
最终正文只给当前要求的结果、依据和实际限制，不复述历史或本轮的执行说明、尝试顺序、脚本修正及自我指令。历史过程不是本轮执行证据。
沙箱脚本、中间文件和分析输出均为内部临时数据；不向用户展示其路径、输出文件段落或使用说明，不把临时保存宣称为文件交付。仅受控成果工具确认发布后，才说明文件可在文件卡片打开或下载。
按字段应用已注入的个人偏好：本轮明确指定的语言、详略、语气、引用或格式覆盖对应默认值，未指定的字段继续使用个人偏好；无偏好时再按问题选择合适表达。按需采用适用的个人Skill方法。当前用户要求优先，偏好和Skill不能授予权限或改变目标。
""" + GOAL_GUIDANCE

DELIVERY_GUIDANCE = """本轮文件交付：
对已确定的文件任务，使用当前可用且获授权的成果工具交付真实文件：新建用 artifact_generate，修订已有成果用 artifact_revise，格式转换用 artifact_convert；机构模板仅在已获授权时使用 template_fill_docx。工具仅实现已确定的目标，不凭文件类型或正文格式建立额外交付任务。
按用户明确的格式、内容、数量、来源和范围交付，内部中间文件不主动发布为成果。多个来源逐一核对读取依据，逐份修订要求逐份交付；转换成功或一个文件成功不代表全部完成。不可用输入不以空白或占位成果代替。
文件交付说明与参数说明分开：技能、系统约定和工具字段用于执行，不是待交付正文。确认文件已发布时可答“文件已生成，可在文件卡片中打开或下载。”，按需说明实际修改；未确认时不能宣称成功。不编造下载链接或客户端行为，下载已有文件不要求重新生成。
只有所需成果实际可用且必要步骤已确认才能说完成；额外成果不能替代原要求，出现时说明实际结果与要求的差异，不编造原因。用户只要文件时不重复全文，要求正文或技术说明时完整回答。
"""

RECOVERY_GUIDANCE = """本轮执行恢复：
依据同一操作的执行确认度、诊断、进展和剩余预算恢复。已确认未执行或确定失败且允许恢复时才修正重试；权限拒绝、retryable=false 或预算耗尽停止相应操作。正在执行或结果未知时先核对状态，不重复提交；断流不代表取消，取消不撤销已完成动作。
保留原目标，不换格式、内容或新增成果冒充恢复。缺口只影响依赖它的部分，独立可验证部分继续。区分可用结果、具体缺项和待确认状态；经核验恢复后不沿用旧失败文案。
"""

OPERATION_GUIDANCE = """本轮工具操作：
工具调用和结果只表示执行进展，不新增用户目标。后续步骤依据当前用户要求及仍有效的历史要求确定。
已明确的交付继续完成；未约定的额外交付，不因已经尝试而变成必须完成的任务。
已发生的操作、结果和未完成部分如实保留；结果未知先核对，不把尝试或未执行当作成功。
"""

_TURN_CONTEXT_NAME = "bank_runtime_turn_context"
_DELIVERY_TOOLS = frozenset({"artifact_generate", "artifact_revise", "artifact_convert", "template_fill_docx", "chart_generate", "chart_export"})
_FILE_CONTROL_TOOLS = frozenset({'runtime_sandbox_files_search', 'runtime_sandbox_files_select'})


def _is_internal_read_conversion(block: ToolCallBlock) -> bool:
    if block.name != 'artifact_convert':
        return False
    try:
        payload = json.loads(block.input) if isinstance(block.input, str) else block.input
    except (ValueError, TypeError):
        return False
    return isinstance(payload, dict) and payload.get('purpose') == 'read'


def _project_file_control_history(messages):
    """Current task facts replace historical selection and internal-copy RPCs.

    Stable identities have already been extracted from the loaded session by
    the sandbox hook. This model-only copy leaves stored prose/audit unchanged.
    """
    last_user = max((i for i, msg in enumerate(messages) if msg.role == 'user'), default=-1)
    expired_ids = set()
    for message in messages[:max(last_user, 0)]:
        for block in message.content:
            if not isinstance(block, ToolCallBlock):
                continue
            if block.name in _FILE_CONTROL_TOOLS or _is_internal_read_conversion(block):
                expired_ids.add(block.id)
    projected = []
    for i, message in enumerate(messages):
        if i >= last_user or message.role not in {'assistant', 'tool'}:
            projected.append(message)
            continue
        content = [block for block in message.content if not (
            isinstance(block, (ToolCallBlock, ToolResultBlock))
            and (block.name in _FILE_CONTROL_TOOLS or block.id in expired_ids))]
        if len(content) == len(message.content):
            projected.append(message)
        elif content:
            item = copy(message)
            item.content = content
            projected.append(item)
    return projected


FOLLOWUP_GUIDANCE = """回答尾部的可选推荐追问（平台交互字段，不属于正文）：
先完成本轮回答，再判断是否存在与当前内容直接相关、尚未回答、用户值得继续了解的下一步。有这样的下一步时，输出1至3条推荐；不要等待用户专门要求推荐。
按实际内容选择，不重复已经完成的内容。每条使用用户视角：用户点击后原样作为下一轮消息发送，应是明确的短动作式选项，每条直接说明要执行的动作和对象。
直接写“细化实施步骤”“解释判断依据”“评估适用条件”等请求。不写助手视角的征询句，不用“需要我……吗”“是否需要……”“要不要……”或其他询问用户意愿的表达；需进一步判断的业务问题也写成评估、比较、解释等具体动作，不把是非问句当作选项。
每条按“动作＋明确对象或范围”起草，如“比较两种方案的成本”“列出异常记录”“解读截图中的关键信息”。输出前逐条检查：点击后能直接发起下一步，不需要用户再回答愿不愿意；若候选仍是征询、能力自述或条件式邀约，改成含义明确的请求，无法确定动作就省略。推荐不带问号或疑问语气词；这不限制本轮正文中的正常问答和必要澄清。
正文末尾不要再写“需要我……”或重复推荐列表；推荐只放在下一行的保留格式中：
<bank_followups>["明确的动作和对象"]</bank_followups>
使用JSON字符串数组，不用代码围栏；该字段由平台分离为按钮，既不是工具调用，也不是新增用户输入。不要为追问调用工具、查找文件或另起模型请求。
每条2至80字，不含Markdown、链接、内部路径、标识、令牌或敏感个人数据。不默认推荐生成文件，不猜测文件内容，不承诺未经确认的执行能力。
没有合适追问、简单确认或已完整解决的封闭问题、失败或拒绝、只有等待补充必要信息的澄清时，完全省略该行；不要为了凑数推荐“继续”或“其他用途”。中间执行说明和思考中不输出该字段。
"""


def prepare_public_model_context(
    request: dict[str, Any], *, delivery_required: bool = False, recovering: bool = False,
    file_context: str = '',
    project_file_history: bool = False,
) -> dict[str, Any]:
    # Replace only our owned ephemeral layer. Profile, personal catalog, activated
    # Skill results and all other system sections remain untouched.
    messages = [msg for msg in request.get("messages") or []
                if not (msg.role == "system" and msg.metadata.get("bank_runtime_layer") == _TURN_CONTEXT_NAME)]
    if project_file_history:
        messages = _project_file_control_history(messages)
    last_user = max((index for index, msg in enumerate(messages) if msg.role == "user"), default=-1)
    # Observed tool operations never establish a delivery requirement. Match
    # internal conversions by current call ID, not by prose or result content.
    current_blocks = [block for message in messages[last_user + 1:] for block in message.content]
    read_conversion_ids = {block.id for block in current_blocks
                           if isinstance(block, ToolCallBlock) and _is_internal_read_conversion(block)}
    artifact_operation_observed = False
    for block in current_blocks:
        if isinstance(block, ToolCallBlock) and block.name in _DELIVERY_TOOLS:
            artifact_operation_observed |= not _is_internal_read_conversion(block)
        elif isinstance(block, ToolResultBlock) and block.name in _DELIVERY_TOOLS:
            artifact_operation_observed |= not (block.name == 'artifact_convert' and block.id in read_conversion_ids)
        if isinstance(block, ToolResultBlock) and block.state in {ToolResultState.ERROR, ToolResultState.DENIED}:
            recovering = True
    for index, message in enumerate(messages[:max(last_user, 0)]):
        if message.role != "assistant":
            continue
        content = []
        for block in message.content:
            # Historical planning can describe obsolete tools or fallback paths.
            # Keep current-turn thinking (including provider signatures) intact.
            if isinstance(block, ThinkingBlock):
                continue
            if isinstance(block, ToolResultBlock) and block.state == ToolResultState.DENIED:
                block = copy(block)
                block.output = [TextBlock(text="此前该操作未获准执行。这是历史结果，不代表本轮已尝试或当前能力。")]
            content.append(block)
        projected = copy(message)
        projected.content = content
        messages[index] = projected

    names = sorted({schema["function"]["name"] for schema in request.get("tools") or []
                    if isinstance(schema, dict) and isinstance(schema.get("function"), dict)
                    and isinstance(schema["function"].get("name"), str)})
    first_text = messages[0].get_text_content() if messages else ""
    guidance = "" if PUBLIC_RESPONSE_GUIDANCE in first_text else PUBLIC_RESPONSE_GUIDANCE
    if guidance and messages and messages[0].role == "system":
        first = copy(messages[0])
        first.content = [TextBlock(text=first_text + "\n\n" + guidance),
                         *(block for block in first.content if not isinstance(block, TextBlock))]
        messages[0] = first
    elif guidance:
        messages.insert(0, SystemMsg(name="system", content=guidance))
    # Keep the per-call reminder next to this turn rather than before a long
    # restored history. The static contract stays in the initial system prompt.
    conditional = DELIVERY_GUIDANCE if delivery_required else (OPERATION_GUIDANCE if artifact_operation_observed else "")
    conditional += RECOVERY_GUIDANCE if recovering else ""
    messages.append(SystemMsg(name="system", metadata={"bank_runtime_layer": _TURN_CONTEXT_NAME},
                              content=CURRENT_TURN_GUIDANCE + conditional +
                              ('\n' + file_context if file_context else '') +
                              "\n本轮可调用入口：" + json.dumps(names, ensure_ascii=False) + "\n\n" + FOLLOWUP_GUIDANCE))
    return {**request, "messages": messages}
