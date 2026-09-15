"""Recover plain DOCX drafting without asking the model to encode long JSON."""
from copy import copy
import json
from uuid import uuid4
from typing import Any

from agentscope.message import SystemMsg, TextBlock, ToolCallBlock
from agentscope.model import ChatResponse
from agentscope.tool import ToolChoice


DRAFT_INSTRUCTION = """上一次文件参数生成异常。现在只完成写作，不调用工具、不宣告文件已生成。
首个非空行必须是一个简短JSON对象，且仅含title、document_type、layout_kind。
document_type仅可为article/report/letter/request/notice/work_plan/task_list/other；
layout_kind按写作技能和用户要求判断为standard_document或official_document，不擅自降级。
随后空一行，直接给出完整正文，不把正文放进JSON，不用代码围栏，不输出工具参数。
必须保留用户要求的事实、篇幅与章节，写够要求的字数，不以摘要代替。
用户指定字数时，先在内部按章节分配篇幅，再逐节充分展开；字数指正文实际文字，
不含这行元数据、标题或字数说明，不能将token数当成中文字数。不要虚构事实或来源。
程序会在正文完成后通过原有受控文件流程生成文档，本次回复本身不是已交付文件。"""
DOCUMENT_TYPES = frozenset({'article', 'report', 'letter', 'request', 'notice', 'work_plan', 'task_list', 'other'})


def draft_request(kwargs: dict[str, Any]) -> dict[str, Any]:
    return {**kwargs, 'tools': [], 'tool_choice': ToolChoice(mode='none'),
            'messages': [*list(kwargs.get('messages') or []), SystemMsg(name='system', content=DRAFT_INSTRUCTION)]}


def controlled_docx_call(response: ChatResponse) -> ChatResponse:
    """Build parameters only; execution must still pass the normal Gateway."""
    if not response.is_last or any(isinstance(b, ToolCallBlock) for b in response.content):
        raise ValueError('DOCX draft did not complete as text')
    text = ''.join(b.text for b in response.content if isinstance(b, TextBlock)).strip()
    header, separator, body = text.partition('\n')
    if not separator or len(header) > 1024 or not body.strip():
        raise ValueError('DOCX draft metadata or body is missing')
    meta = json.loads(header)
    if not isinstance(meta, dict) or set(meta) != {'title', 'document_type', 'layout_kind'}:
        raise ValueError('DOCX draft metadata is invalid')
    if (not isinstance(meta['title'], str) or not meta['title'].strip() or len(meta['title']) > 200
            or not isinstance(meta['document_type'], str)
            or meta['document_type'] not in DOCUMENT_TYPES or meta['layout_kind'] != 'standard_document'):
        raise ValueError('DOCX draft requires a supported ordinary document')
    payload = {'artifact_type': 'docx', 'title': meta['title'], 'content': body.strip(),
               'delivery_plan': {'document_type': meta['document_type'], 'target_format': 'docx',
                                 'layout_kind': 'standard_document'}}
    arguments = json.dumps(payload, ensure_ascii=False)
    if len(arguments.encode('utf-8')) > 2 * 1024 * 1024:
        raise ValueError('DOCX draft exceeds the request budget')
    result = copy(response)
    result.content = [ToolCallBlock(id='call_docx_'+uuid4().hex, name='artifact_generate', input=arguments)]
    return result
