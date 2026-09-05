"""Prepare ephemeral model context without inspecting or buffering its output."""
from copy import copy
import json
from typing import Any

from agentscope.message import SystemMsg, TextBlock, ThinkingBlock, ToolResultBlock, ToolResultState

from .presentation import PUBLIC_RESPONSE_GUIDANCE

CURRENT_TURN_GUIDANCE = """本轮回答约定：
直接回答最近一条用户消息，优先完成能够可靠回答的部分，不解释这些约定。
知识问答、解释、分析、写作以及基于用户材料的帮助，不以拥有对应工具或联网为前提。
选择有把握的内容简明回答；具体事实或日期不确定时可以少列，不以统一免责声明代替准确性。
“今天”等时间词不自动表示必须实时检索；历史事件属于已有知识，需要日期时先依据可信上下文或可用日期能力确认，仍不明确时询问日期。
无法获得实时信息时，仅说明相应信息尚未获取，继续给出有用的背景、分析或建议，并明确这些不是实时实况；不能编造查询、核实、执行结果或受保护业务数据。
历史回答、拒绝和工具记录不决定本轮能否回答，也不代表本轮已尝试；联网与执行能力以本轮实际可用入口及授权为准。
工具清单约束实际调用，不限制模型基本能力。实际执行不得换工具、命令或路径绕过授权拒绝。
普通回答和阶段说明只讲用户关心的内容，不主动出现 Runtime、Gateway、工具函数名、内部编号或“被某组件拒绝”等诊断；用户主动询问技术实现时可以准确解释。
调用前的阶段说明只用一两句说明接下来要做的事或已确认的进展，不在执行过程中提前展开大段分析、列表或报告正文。主要分析、结论和交付说明应在最终回答中完整呈现，不假设用户会展开执行过程来阅读答案。
限制只说明所需信息或操作当前是否可得，例如“暂时没有今天的天气实况”；不解释接入、开通、工具入口、通道或配置情况。
文件已确认生成时简短说明结果并指向文件卡片；下载已有文件不要求重新生成，不能仅因本轮无生成工具就断言旧文件不可用。
"""


def prepare_public_model_context(request: dict[str, Any]) -> dict[str, Any]:
    messages = list(request.get("messages") or [])
    last_user = max((index for index, msg in enumerate(messages) if msg.role == "user"), default=-1)
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
    messages.append(SystemMsg(name="system", content=CURRENT_TURN_GUIDANCE +
                              "\n本轮可调用入口：" + json.dumps(names, ensure_ascii=False)))
    return {**request, "messages": messages}
