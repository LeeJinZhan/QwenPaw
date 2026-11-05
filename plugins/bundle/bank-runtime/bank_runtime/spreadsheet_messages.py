"""Compatibility projections of the versioned Office message catalog.

The catalog and capabilities are separate: messages never authorize execution.
"""

from .office_assets import office_asset

# Existing names/reasons remain compatible with installed consumers.
SPREADSHEET_FAILURES = {key: tuple(value) for key, value in office_asset('messages')['failures'].items()}


def office_failure_hint(code):
    return office_asset('messages')['failure_codes'].get(code, '')


def spreadsheet_failure_hint(code: str, *, reason: str = '', message: str = '') -> str:
    for key, (expected_code, text) in SPREADSHEET_FAILURES.items():
        if code == expected_code and (reason == key or message == text):
            return text
    return ''


def spreadsheet_terminal_hint(diagnostic: str) -> str:
    prefix = 'SPREADSHEET_OPERATION|'
    if isinstance(diagnostic, str) and diagnostic.startswith(prefix):
        entry = SPREADSHEET_FAILURES.get(diagnostic[len(prefix):])
        return entry[1] if entry else ''
    return ''


def spreadsheet_envelope_failure(envelope):
    from collections.abc import Mapping
    if not isinstance(envelope, Mapping):
        return '', ''
    code = envelope.get('error_code') or envelope.get('code', '')
    candidates = [envelope]
    for field in ('result', 'details', 'error_details'):
        value = envelope.get(field)
        if isinstance(value, Mapping):
            candidates.append(value)
            if isinstance(value.get('details'), Mapping):
                candidates.append(value['details'])
    for value in candidates:
        reason = value.get('reason', '')
        hint = spreadsheet_failure_hint(code, reason=reason)
        if hint:
            return reason, hint
    return '', ''


def converted_copy_hint(report, *, artifact_type=''):
    """Caller must first validate the conversion report."""
    if report.get('provenance', {}).get('engine') != 'libreoffice':
        return ''
    messages = office_asset('messages')['converted']
    return messages.get(artifact_type, messages['other']) + messages.get('limitations', {}).get(artifact_type, messages['limitation'])


def rebuilt_copy_hint(artifact_type):
    messages = office_asset('messages')
    if artifact_type == 'docx':
        return messages['revision_rebuild']
    return messages.get('revision_rebuild_by_format', {}).get(artifact_type, '')


def revision_copy_hint(report):
    """Caller must validate the format-specific revision report before presentation."""
    if report.get('kind') == 'text_patch':
        from .text_patch_contract import validate_text_revision_report
        validate_text_revision_report(report)
        return office_asset('messages')['text_revision']
    messages = office_asset('messages')['xlsx_revision']
    parts = []
    if report['calculation'] == 'on_open':
        parts.append(messages['on_open'])
    if report['refresh_required']:
        parts.append(messages['refresh_required'])
    return ' '.join(parts)


def recoverable_patch_failure(reason):
    return reason in office_asset('policy')['recoverable_patch_failures']
