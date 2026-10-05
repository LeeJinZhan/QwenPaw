"""Conservative document input binding BEFORE preflight and permit hashing.

Only task-local, already prepared/observed references are eligible. Never infer
sheet names, column names, unknown tokens or absent read targets from a singleton.
"""
from copy import deepcopy
import re

from .document_access import DOCUMENT_TOOLS
from ..sandbox.file_refs import FileRefError, get_file_ref_registry



def document_call_argument_hint(name, arguments):
    if name == 'MinerU__aggregate' and set(arguments) - {'document_ref', 'ops'}:
        return ('统计调用顶层只使用document_ref和ops；numeric_text须位于各ops对象内，'
                '值为strict或thousands，不得与已有操作规则冲突。移除其它未声明顶层字段后重试。')
    return ''


def _integer(value):
    if isinstance(value, str) and re.fullmatch(r'[0-9]{1,10}', value):
        return int(value)
    return value


def normalize_document_input(name, arguments, *, task_id, ledger):
    raw = name.removeprefix('MinerU__')
    if name != 'MinerU__' + raw or raw not in DOCUMENT_TOOLS:
        return arguments
    value = deepcopy(arguments)
    if raw == 'parse_documents':
        documents = value.get('documents')
        if isinstance(documents, list):
            for document in documents:
                if (isinstance(document, dict) and isinstance(document.get('file_id'), str)
                        and document.get('file_ref') in (None, '', document['file_id'])):
                    try:
                        document['file_ref'] = get_file_ref_registry().reference_for_file(
                            document['file_id'], expected_task_id=task_id)
                    except FileRefError:
                        # No new authorization or automatic file selection.
                        pass
        return value
    ref = value.get('document_ref')
    if isinstance(ref, str) and ref not in ledger.documents:
        file_id = ref
        if ref.startswith('fr1_'):
            try:
                file_id = get_file_ref_registry().resolve(ref, expected_task_id=task_id).file_id
            except FileRefError:
                # Keep the explicit invalid capability for the authorization
                # failure path; matching an old ledger alias cannot revive it.
                file_id = None
        candidates = [token for token, doc in ledger.documents.items()
                      if file_id and (doc.file_id == file_id or ledger.source_refs.get(token) == ref)]
        if len(candidates) == 1:
            value['document_ref'] = candidates[0]
    if raw in {'read_range', 'read_document_chunks', 'search'}:
        for key in ('row_cursor', 'limit'):
            if key in value:
                value[key] = _integer(value[key])
        if isinstance(value.get('rows'), list):
            value['rows'] = [_integer(item) for item in value['rows']]
        if value.get('include_header') in ('true', 'false'):
            value['include_header'] = value['include_header'] == 'true'
    if raw == 'read_document_chunks':
        # A first-page zero is not an opaque continuation capability. Normalize
        # only this unambiguous alias; never construct/repair signed cursors.
        cursor = value.get('cursor')
        if (type(cursor) is int and cursor == 0) or (isinstance(cursor, str) and cursor in {'', '0'}):
            value['cursor'] = None
        # Reduce page size to the service budget before permission hashing,
        # execution and coverage accounting, without changing the target.
        if type(value.get('limit')) is int and value['limit'] > 10:
            value['limit'] = 10
    return value
