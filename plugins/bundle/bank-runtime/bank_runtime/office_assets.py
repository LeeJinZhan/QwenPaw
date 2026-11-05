"""Versioned Office presentation and execution resources, owned by Runtime.

Other process packages receive generated copies through sync_office_assets.py.
These resources never grant a tool permission or add a renderer capability.
"""
from copy import deepcopy
from functools import lru_cache
from importlib.resources import files
import json

_NAMES = frozenset({'messages', 'policy', 'xlsx_styles', 'word_styles', 'ppt_themes'})


@lru_cache(maxsize=5)
def _load(name):
    if name not in _NAMES:
        raise ValueError('Office resource is not registered')
    value = json.loads(files(__package__).joinpath('office_resources', name + '.json').read_text(encoding='utf-8'))
    if not isinstance(value, dict) or value.get('version') != 1:
        raise ValueError('Office resource version is unsupported')
    return value


def office_asset(name):
    return deepcopy(_load(name))


def execution_budget(section):
    if section not in {'recovery', 'workbook', 'patch', 'text_patch'}:
        raise ValueError('Office budget is not registered')
    result = office_asset('policy')[section]
    if not isinstance(result, dict) or any(type(v) is not int or not 1 <= v <= 1000000 for v in result.values()):
        raise ValueError('Office budgets must be bounded positive integers')
    return result


def csv_options(value=None):
    policy = office_asset('policy')['csv']
    if value is None:
        value = {}
    if not isinstance(value, dict) or set(value) - {'encoding', 'delimiter', 'line_ending'}:
        raise ValueError('CSV options only accept encoding, delimiter and line_ending')
    result = {**policy['defaults'], **value}
    for key, allowed in [('encoding', policy['encodings']), ('delimiter', policy['delimiters']), ('line_ending', policy['line_endings'])]:
        if not isinstance(result[key], str) or result[key] not in allowed:
            raise ValueError('CSV option is unsupported: ' + key)
    return {**result, 'lineterminator': policy['line_endings'][result['line_ending']]}


def quality_warnings(value):
    allowed = office_asset('messages')['quality']
    if not isinstance(value, list) or len(value) > len(allowed) or any(not isinstance(v, str) or v not in allowed for v in value) or len(set(value)) != len(value):
        raise ValueError('Office quality report is invalid')
    return list(value)
