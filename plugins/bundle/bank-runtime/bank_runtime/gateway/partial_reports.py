"""Mark independently supported report drafts before their normal Gateway admission."""
from copy import deepcopy
import json
from html import escape
from pathlib import PurePath


def partial_report_input(name, payload, intent, ledger, source_aliases=None):
    if (name != 'artifact_generate' or intent is None or intent.operation != 'generate'
            or intent.input_scope == 'complete' or not ledger.unfinished_scopes()):
        return None
    content = payload.get('content')
    text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    if not ledger.permits_scoped_answer(text):
        return None
    # Preserve the existing format schemas. Other formats retain their normal completion guard.
    fmt = payload.get('artifact_type')
    if fmt not in {'docx', 'markdown', 'txt', 'html', 'xlsx', 'pptx', 'csv'}:
        return None
    refs = list(intent.source_refs)
    gaps = [{**gap, 'file_id': (source_aliases or {}).get(gap['file_id'], gap['file_id'])}
            for gap in ledger.unfinished_scopes()]
    if not refs or any(gap['file_id'] not in refs for gap in gaps):
        return None
    labels = []
    for gap in gaps:
        label = f"第{refs.index(gap['file_id']) + 1}份材料"
        if gap.get('target'):
            label += f"的工作表“{gap['target']}”"
        labels.append(label)
    notice = ('部分稿：尚未完成' + '、'.join(labels) + '的读取与分析。'
              '本稿仅适用于已确认范围，不能用于全范围合计、排名或排除性判断。')
    result = deepcopy(payload)
    if fmt == 'html' and (isinstance(content, str) or isinstance(content, dict) and set(content) == {'text'}):
        html = content if isinstance(content, str) else content['text']
        prefix = '<p>' + escape(notice) + '</p>'
        import re
        marked = re.sub(r'(<body\b[^>]*>)', lambda match: match[0] + prefix, html, count=1, flags=re.I) if re.search(r'<body\b', html, re.I) else prefix + html
        result['content'] = marked if isinstance(content, str) else {'text': marked}
    elif fmt in {'markdown', 'txt'} and isinstance(content, dict) and set(content) == {'text'}:
        result['content']['text'] = notice + '\n\n' + content['text']
    elif fmt == 'xlsx' and isinstance(content, dict) and isinstance(content.get('sheets'), list):
        names = {sheet.get('name') for sheet in content['sheets'] if isinstance(sheet, dict)}
        name = '部分稿范围说明'
        while name in names: name += '_'
        result['content']['sheets'].insert(0, {'name': name, 'rows': [[notice]]})
    elif fmt == 'pptx' and isinstance(content, dict) and isinstance(content.get('slides'), list):
        result['content']['slides'].insert(0, {'title': '部分稿范围说明', 'bullets': [notice]})
    elif fmt == 'csv' and isinstance(content, dict) and isinstance(content.get('rows'), list):
        width = max([len(content.get('columns') or [])] + [len(row) for row in content['rows'] if isinstance(row, list)])
        result['content']['rows'].insert(0, [notice] + [''] * max(0, width - 1))
    elif isinstance(content, str) and fmt in {'docx', 'markdown', 'txt'}:
        result['content'] = notice + '\n\n' + content
    elif fmt == 'docx' and isinstance(content, dict):
        if isinstance(content.get('paragraphs'), list):
            result['content']['paragraphs'].insert(0, notice)
        elif isinstance(content.get('sections'), list):
            result['content']['sections'].insert(0, {'heading': '部分稿范围说明', 'paragraphs': [notice]})
        elif isinstance(content.get('document'), dict) and isinstance(content['document'].get('blocks'), list):
            result['content']['document']['blocks'].insert(0, {'type': 'paragraph', 'text': notice})
        else:
            return None
    else:
        return None
    result['title'] = str(payload.get('title') or '分析报告') + '（部分稿）'
    if payload.get('output_name'):
        output = PurePath(str(payload['output_name']))
        result['output_name'] = str(output.with_name(output.stem + '（部分稿）' + output.suffix))
    return result
