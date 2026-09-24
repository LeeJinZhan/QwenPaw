"""Recover a complete PPT proposal; execution still belongs to the Gateway."""
from copy import copy
import json
from pathlib import Path
from uuid import uuid4
from agentscope.message import SystemMsg, TextBlock, ToolCallBlock
from agentscope.tool import ToolChoice

INSTRUCTION = """上一轮没有得到有效的PPT工具参数。现在只起草参数，不调用工具，不宣告文件已生成。
只返回一个完整JSON对象，顶层仅含title和content，不使用代码围栏。
content沿用银行演示文稿技能的PPT结构，必须包含非空slides数组；每页包含layout与title。
例如：{"title":"演示初稿","content":{"theme":"steady_business","slides":[{"layout":"cover","title":"演示初稿"},{"layout":"content","title":"主要内容","bullets":["待补充"]}]}}
依据当前用户要求和已经确认的信息组织全部页面，不复制示例替代已提供的内容。
保留用户明确的页数、主题和事实。仅要求无主题初稿时可制作明确标记待补充的通用结构稿，不虚构机构、业绩或数据。
不得编造图片、文件引用或路径。保持页面文字简洁，但不得用缩短页数或丢弃用户要求来规避限制。
程序将校验参数并通过原有受控工具生成文件；这段JSON本身不是交付成果。"""


def draft_request(kwargs):
    # Fixed bundled guidance, never a user/model-selected filesystem path.
    guidance = (Path(__file__).resolve().parents[1] / 'skills' / 'bank-presentation' / 'SKILL.md').read_text(encoding='utf-8')
    return {**kwargs, 'tools': [], 'tool_choice': ToolChoice(mode='none'),
            'messages': [*list(kwargs.get('messages') or []), SystemMsg(name='system',content=guidance+'\n\n'+INSTRUCTION)]}


def controlled_pptx_call(response):
    if not response.is_last or any(isinstance(b,ToolCallBlock) for b in response.content):
        raise ValueError('PPT draft is not a completed text response')
    text=''.join(b.text for b in response.content if isinstance(b,TextBlock)).strip()
    if len(text.encode('utf-8')) > 2*1024*1024:
        raise ValueError('PPT draft exceeds request budget')
    # Ignore prose/fences surrounding one complete proposal, but never join
    # fragments or select between multiple conflicting JSON objects.
    start = text.find('{')
    if start < 0:
        raise ValueError('PPT draft JSON is missing')
    value, end = json.JSONDecoder().raw_decode(text[start:])
    outside = text[:start] + text[start+end:]
    if any(char in outside for char in '{}[]'):
        raise ValueError('PPT draft contains ambiguous structured output')
    if not isinstance(value,dict) or set(value)!= {'title','content'}:
        raise ValueError('PPT draft fields are invalid')
    if not isinstance(value['title'],str) or not value['title'].strip() or len(value['title'])>200:
        raise ValueError('PPT title is invalid')
    content=value['content']
    slides=content.get('slides') if isinstance(content,dict) else None
    if not isinstance(slides,list) or not 1<=len(slides)<=100:
        raise ValueError('PPT slides are missing or exceed limit')
    if any(not isinstance(s,dict) or not isinstance(s.get('title'),str) or not s['title'].strip()
           or not isinstance(s.get('layout'),str) or not s['layout'].strip() for s in slides):
        raise ValueError('PPT slide is incomplete')
    # Full layout/content validation, resource checks and publication remain in Runtime.
    result=copy(response)
    result.content=[ToolCallBlock(id='call_pptx_'+uuid4().hex,name='artifact_generate',
        input=json.dumps({'artifact_type':'pptx',**value},ensure_ascii=False))]
    return result
