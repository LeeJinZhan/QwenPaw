"""Explicit, bounded Word/PowerPoint text edits; mirrored into consumers."""
import re

from .office_assets import execution_budget


def is_text_patch(content):
    return isinstance(content, dict) and content.get('kind') in ('docx_patch', 'pptx_patch')


def validate_text_patch(content, artifact_type):
    limits = execution_budget('text_patch')
    if (artifact_type not in ('docx', 'pptx') or not isinstance(content, dict)
            or set(content) != {'kind', 'edits'} or content['kind'] != artifact_type + '_patch'):
        raise ValueError('Text patch requires matching kind and edits')
    edits = content['edits']
    if not isinstance(edits, list) or not 1 <= len(edits) <= limits['max_operations']:
        raise ValueError('Text patch operation count exceeds capacity')
    for edit in edits:
        if not isinstance(edit, dict) or set(edit) != {'target', 'old_text', 'new_text'}:
            raise ValueError('Each text edit requires target, old_text and new_text')
        for key in ('old_text', 'new_text'):
            value = edit[key]
            if (not isinstance(value, str) or len(value) > limits['max_text_length']
                    or re.search(r'[\x00-\x1f\ud800-\udfff\ufffe\uffff]', value)):
                raise ValueError('Text edit requires single-paragraph XML text within capacity')
        if not edit['old_text'] or edit['old_text'] == edit['new_text']:
            raise ValueError('Text edit requires nonempty original text and a change')
        target = edit['target']
        if not isinstance(target, dict) or any(type(v) is not int or not 1 <= v <= 1000000 for v in target.values()):
            raise ValueError('Text edit locators must be positive integer positions')
        keys = set(target)
        if artifact_type == 'docx':
            if keys not in (set(), {'paragraph'}, {'table', 'row', 'column'}):
                raise ValueError('Word target requires paragraph or table/row/column')
        elif keys not in ({'slide'}, {'slide', 'shape_id'}, {'slide', 'shape_id', 'row', 'column'}):
            raise ValueError('PowerPoint target requires slide, optional shape_id and table row/column')
    return content


def validate_text_revision_report(report):
    if (not isinstance(report, dict)
            or set(report) != {'kind', 'artifact_type', 'changed_texts', 'layout', 'preservation'}
            or report['kind'] != 'text_patch'
            or report['artifact_type'] not in ('docx', 'pptx')
            or type(report['changed_texts']) is not int
            or not 1 <= report['changed_texts'] <= execution_budget('text_patch')['max_operations']
            or report['layout'] != 'unverified'
            or report['preservation'] != 'untouched_parts_verified'):
        raise ValueError('Invalid text revision report')
    return dict(report)
