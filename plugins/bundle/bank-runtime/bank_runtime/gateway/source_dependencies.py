"""Resolve declared file dependencies without inferring them from prose.

This is a dependency guard, not authorization. Runtime still validates every
reference and permit. Missing dependency metadata is unknown, never an empty set.
"""
from collections.abc import Mapping


def operation_sources(name, payload, intent=None):
    if name == 'chart_export' and payload.get('chart_id'):
        # Export consumes a persisted chart version, not the current read ledger.
        # Runtime checks chart ownership/version integrity before execution.
        return set()
    sources = set()
    for key in ('source_id', 'source_generated_file_id', 'file_id'):
        if isinstance(payload.get(key), str) and payload[key]:
            sources.add(payload[key])
    refs = payload.get('source_refs')
    if isinstance(refs, list):
        for ref in refs:
            if not isinstance(ref, Mapping) or not isinstance(ref.get('source_id'), str) or not ref['source_id']:
                return None
            sources.add(ref['source_id'])
    if sources:
        return sources
    # Only trusted task intent may establish that a delivery has no file input.
    # An empty model-supplied list must not erase known read dependencies.
    if intent is not None and name in {'artifact_generate', 'template_fill_docx'}:
        return set(intent.source_refs)
    return None


def affected_sources(sources, gaps, converted_sources):
    if not gaps:
        return False
    if sources is None:
        return True
    def original(value):
        seen = set()
        while value in converted_sources and value not in seen:
            seen.add(value)
            value = converted_sources[value]
        return value
    return bool({original(value) for value in sources} & {original(value) for value in gaps})
