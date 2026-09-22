"""Request-local evidence for attempted file operations, never model prose."""
from collections.abc import Mapping
import json


def operation_keys(name, payload):
    if name.endswith("parse_documents"):
        return {"parse:" + str(item.get("file_id") or item.get("file_ref"))
                for item in payload.get("documents", []) if isinstance(item, Mapping)}
    if name not in {"artifact_generate", "artifact_revise", "artifact_convert", "template_fill_docx"}:
        return set()
    # Full payload and tool identity prevent same-name/source operations from
    # erasing one another. Parameter rejections are tracked separately because
    # no file operation has started at preflight.
    from .protocol import canonical_payload_hash
    return {f"artifact:{name}:{canonical_payload_hash(payload)}"}


def _result_values(text):
    """Decode complete JSON values merged by AgentScope, without extracting prose."""
    if not isinstance(text, str):
        return []
    decoder = json.JSONDecoder()
    values = []
    position = 0
    try:
        while position < len(text):
            if text[position] in " \t\r\n":
                position += 1
                continue
            value, position = decoder.raw_decode(text, position)
            values.append(value)
    except ValueError:
        # A valid prefix is insufficient evidence if the result is truncated.
        return []
    return values


def parse_outcomes(content):
    """Read parser item statuses, retaining failures when duplicate evidence conflicts."""
    outcomes = {}
    for block in content or []:
        kind = block.get("type") if isinstance(block, Mapping) else getattr(block, "type", "")
        text = block.get("text", "") if isinstance(block, Mapping) else getattr(block, "text", "")
        if kind != "text":
            continue
        # MCP may return the same JSON in content and structuredContent. The
        # driver emits both and AgentScope joins adjacent TextBlocks without a
        # separator, so a successful result can contain multiple JSON values.
        for value in _result_values(text):
            if not isinstance(value, Mapping) or not isinstance(value.get("items"), list):
                continue
            for item in value["items"]:
                if isinstance(item, Mapping) and item.get("file_id"):
                    key = "parse:" + str(item["file_id"])
                    outcomes[key] = outcomes.get(key, True) and item.get("status") == "completed"
    yield from outcomes.items()
