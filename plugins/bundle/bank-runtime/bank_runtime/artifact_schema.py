"""Model-facing DOCX field guidance; Runtime remains the validation authority."""
from copy import deepcopy
from collections.abc import Mapping
from typing import Any

from .artifact_tools import ArtifactDeliveryIntent


def docx_retry_schema_hint(tool_name: str, payload: Mapping[str, Any]) -> ArtifactDeliveryIntent | None:
    """Describe a rejected proposal; this never creates trusted task authority."""
    operation = {'artifact_generate': 'generate', 'artifact_revise': 'revise'}.get(tool_name)
    if not operation or (operation == 'generate' and payload.get('artifact_type') != 'docx'):
        return None
    plan = payload.get('delivery_plan')
    if not isinstance(plan, Mapping) or plan.get('target_format') != 'docx':
        return None
    layout = plan.get('layout_kind')
    if layout not in ('official_document', 'standard_document'):
        return None
    return ArtifactDeliveryIntent(operation, 'docx', layout_kind=layout, layout_resolution='skill')


def describe_docx_tools(tools: Any, intent: ArtifactDeliveryIntent | None) -> Any:
    """Describe both supported DOCX layouts without guessing unresolved intent.

    Keep Runtime input compatibility and never alter tool admission or
    overwrite the shared toolkit schemas. Layout selection remains the writing skill's responsibility when not explicit.
    """
    if intent is None or intent.target_format != 'docx':
        return tools
    result = deepcopy(tools)
    strings = {'type': 'array', 'items': {'type': 'string'}}
    table = {'type': 'array', 'items': strings}
    tables = {'type': 'array', 'items': table}
    section = {'type': 'object', 'additionalProperties': False, 'properties': {
        'heading': {'type': 'string'},
        'heading_level': {'type': 'integer', 'minimum': 1, 'maximum': 6},
        'paragraphs': strings, 'bullets': strings, 'tables': tables,
    }}
    for schema in result or []:
        function = schema.get('function', {})
        if function.get('name') not in {'artifact_generate', 'artifact_revise'}:
            continue
        properties = function.get('parameters', {}).get('properties', {})
        if 'artifact_type' in properties:
            properties['artifact_type'] = {'type': 'string', 'enum': ['docx']}
        content = properties.get('content', {})
        for branch in content.get('anyOf', [content]):
            if branch.get('type') == 'object':
                branch['additionalProperties'] = False
                branch['properties'] = {
                    'paragraphs': strings,
                    'sections': {'type': 'array', 'items': section},
                    'tables': tables,
                }
                branch['description'] = '完整普通 Word 正文。章节用 sections，每节包含 heading 和 paragraphs；不得再次编码成 JSON 字符串。'
                standard = branch
                official = _official_content_schema()
                properties['content'] = (standard if intent.layout_kind == 'standard_document' else official if intent.layout_kind == 'official_document' else {'anyOf': [standard, official]})
                break
        if 'delivery_plan' in properties:
            properties['delivery_plan'] = {'type': 'object', 'additionalProperties': False, 'properties': {
                'document_type': {'type': 'string', 'enum': [
                    'letter', 'request', 'notice', 'report', 'work_plan', 'task_list', 'article', 'other',
                ]},
                'target_format': {'type': 'string', 'enum': ['docx']},
                'layout_kind': {'type': 'string', 'enum': [intent.layout_kind] if intent.layout_kind else ['official_document', 'standard_document']},
            }, 'required': ['document_type', 'target_format', 'layout_kind']}
            parameters = function['parameters']
            required = parameters.setdefault('required', [])
            if intent.layout_resolution == 'skill' and 'delivery_plan' not in required:
                required.append('delivery_plan')
    return result


def _official_content_schema() -> dict[str, Any]:
    text = {'type': 'string'}
    texts = {'type': 'array', 'items': text}
    paragraph = {'type': 'object', 'additionalProperties': False,
                 'properties': {'type': {'type': 'string', 'enum': ['paragraph']}, 'text': text},
                 'required': ['type', 'text']}
    heading = {'type': 'object', 'additionalProperties': False,
               'properties': {'type': {'type': 'string', 'enum': ['heading']}, 'text': text,
                              'level': {'type': 'integer', 'minimum': 1, 'maximum': 4}},
               'required': ['type', 'text', 'level']}
    table = {'type': 'object', 'additionalProperties': False,
             'properties': {'type': {'type': 'string', 'enum': ['table']}, 'headers': texts,
                            'rows': {'type': 'array', 'items': texts}},
             'required': ['type', 'headers', 'rows']}
    document = {'type': 'object', 'additionalProperties': False, 'properties': {
        'title': text, 'recipients': {**texts, 'description': '主送单位专用字段，排在标题后顶格；不要写进 blocks。初稿缺失时用【主送单位待填写】。'},
        'blocks': {'type': 'array', 'minItems': 1, 'maxItems': 500,
                   'items': {'anyOf': [paragraph, heading, table]}},
        'signatory': {**text, 'description': '落款单位或部门专用字段，右对齐；不要写进 blocks。缺失时用【申请单位待填写】。'},
        'date': {**text, 'description': '成文日期专用字段，排在落款下方；不要写进 blocks。未提供时用【成文日期待填写】，不得擅用当天日期。'},
        'classification': text, 'urgency': text,
        'attachments': texts, 'cc': texts,
    }, 'required': ['title', 'recipients', 'blocks']}
    return {'type': 'object', 'additionalProperties': False, 'properties': {
        'kind': {'type': 'string', 'enum': ['official_document']},
        'layout_version': {'type': 'string', 'enum': ['bank-official-docx-v1']},
        'document': document,
    }, 'required': ['kind', 'layout_version', 'document']}
