"""Non-authorizing file identities from managed conversation history."""
import json
import re
from .scope import MAX_TASK_FILES


def stable_file_metadata(item):
    """Project display identity and optional integrity facts, never capabilities."""
    if not isinstance(item, dict):
        return None
    file_id = item.get('file_id')
    if not isinstance(file_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,160}', file_id):
        return None
    record = {'file_id': file_id, 'display_name': str(item.get('display_name') or '')[:255],
              'content_type': str(item.get('content_type') or '')[:128]}
    size = item.get('size_bytes')
    if type(size) is int and size >= 0:
        record['size_bytes'] = size
    digest = item.get('content_hash')
    if isinstance(digest, str) and re.fullmatch(r'(?:sha256:)?[a-f0-9]{64}', digest):
        record['content_hash'] = digest.removeprefix('sha256:')
    return record


def selected_file_metadata(block):
    """Read stable identities only from a successful registered selection."""
    if (block.get('type') != 'tool_result' or block.get('name') != 'runtime_sandbox_files_select'
            or block.get('state') != 'success'):
        return []
    metadata = block.get('metadata')
    items = list(metadata.get('runtime_selected_file_metadata', [])) if isinstance(metadata, dict) and isinstance(metadata.get('runtime_selected_file_metadata'), list) else []
    for part in block.get('output', []):
        if not isinstance(part, dict) or part.get('type') != 'text':
            continue
        try:
            # SDK can combine the JSON result and private attachment XML into
            # one text block. Decode the bounded JSON prefix, never the body.
            text = part.get('text', '')[:65536].lstrip()
            value, end = json.JSONDecoder().raw_decode(text)
            suffix = text[end:].lstrip()
            if suffix and not suffix.startswith('<runtime_attachment '):
                continue
        except (ValueError, TypeError, AttributeError):
            continue
        if isinstance(value, dict) and isinstance(value.get('selected_files'), list):
            items.extend(value['selected_files'])
    records = {}
    for item in items:
        record = stable_file_metadata(item)
        if record is not None:
            records[record['file_id']] = record
        if len(records) >= MAX_TASK_FILES:
            break
    return list(records.values())




def historical_file_metadata(agent):
    state_dict = getattr(agent, "state_dict", None)
    if not callable(state_dict):
        return {}
    snapshot = state_dict()
    snapshot = snapshot.get("state", snapshot) if isinstance(snapshot, dict) else {}
    records = {}
    for message in reversed(snapshot.get("context", [])):
        if not isinstance(message, dict):
            continue
        items = []
        if message.get("role") == "user":
            metadata = message.get("metadata") or {}
            items = metadata.get("runtime_attachment_metadata", []) if isinstance(metadata, dict) else []
        elif message.get("role") in {"tool", "assistant"}:
            # Selection results contain stable metadata, even for files first
            # discovered in the workspace rather than uploaded in this session.
            for block in message.get("content", []):
                if (not isinstance(block, dict) or block.get("type") != "tool_result"
                        or block.get("name") != "runtime_sandbox_files_select"
                        or block.get("state") != "success"):
                    continue
                items.extend(selected_file_metadata(block))
        for item in items if isinstance(items, list) else []:
            record = stable_file_metadata(item)
            if record is None or record['file_id'] in records:
                continue
            records[record['file_id']] = record
            if len(records) >= MAX_TASK_FILES:
                return records
    return records
