"""Request-local evidence for attempted file operations, never model prose."""
from collections.abc import Mapping
import json


def operation_keys(name, payload):
    if name.endswith("parse_documents"):
        return {"parse:" + str(item.get("file_id") or item.get("file_ref"))
                for item in payload.get("documents", []) if isinstance(item, Mapping)}
    if name not in {"artifact_generate", "artifact_revise", "artifact_convert", "template_fill_docx"}:
        return set()
    source = payload.get("source_generated_file_id") or payload.get("source_id")
    refs = payload.get("source_refs") or []
    if not source and refs:
        source = ",".join(sorted(str(ref.get("source_id") or "") for ref in refs if isinstance(ref, Mapping)))
    target = payload.get("artifact_type") or payload.get("target_format") or name
    # A success for another source/output cannot erase an unresolved failure.
    identity = source or payload.get("output_name") or payload.get("title") or "unspecified"
    return {f"artifact:{target}:{identity}"}


def parse_outcomes(content):
    """Read only the registered parser's structured item statuses."""
    for block in content or []:
        kind = block.get("type") if isinstance(block, Mapping) else getattr(block, "type", "")
        text = block.get("text", "") if isinstance(block, Mapping) else getattr(block, "text", "")
        if kind != "text":
            continue
        try:
            value = json.loads(text)
        except (ValueError, TypeError):
            continue
        if not isinstance(value, Mapping) or not isinstance(value.get("items"), list):
            continue
        for item in value["items"]:
            if isinstance(item, Mapping) and item.get("file_id"):
                yield "parse:" + str(item["file_id"]), item.get("status") == "completed"
