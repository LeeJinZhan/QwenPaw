"""Legacy RAGFlow recall shapes, isolated from the versioned retrieval contract."""
from __future__ import annotations

import hashlib
import html
import json
import re
from typing import Any

_SCOPE_ERRORS = {"MISSING_SCOPE", "MISSING_AGENTNAME", "EMPTY_DEPARTMENT", "MULTI_DEPARTMENT_LOCAL_FILTER_OFF"}


def ragflow_result_error(value: dict) -> str:
    codes = value.get("error_codes")
    code = value.get("error_code")
    if isinstance(code, str) and code in _SCOPE_ERRORS:
        return "MISSING_SCOPE"
    if isinstance(codes, list) and any(isinstance(code, str) and code in _SCOPE_ERRORS for code in codes):
        return "MISSING_SCOPE"
    scope = value.get("department_scope")
    if (value.get("error") or value.get("error_code") or codes
            or (isinstance(scope, dict) and scope.get("error"))):
        return "KNOWLEDGE_RETRIEVAL_FAILED"
    if "code" in value and (value["code"] != 0 or isinstance(value["code"], bool)):
        return "KNOWLEDGE_RETRIEVAL_FAILED"
    return ""


def _text(value: dict, *keys: str) -> str:
    return next((value[key] for key in keys if isinstance(value.get(key), str) and value[key].strip()), "")


def adapt_ragflow_result(value: dict, limit: int) -> tuple[list[dict[str, Any]], str]:
    """Map documented legacy responses; never infer success from HTTP status."""
    error = ragflow_result_error(value)
    if error:
        return [], error
    data = value if "chunks" in value else value.get("data")
    if not isinstance(data, dict):
        return [], "KNOWLEDGE_RESULT_INVALID"
    error = ragflow_result_error(data)
    if error:
        return [], error
    if not isinstance(data.get("chunks"), list):
        return [], "KNOWLEDGE_RESULT_INVALID"
    aggregates = data.get("doc_aggs")
    if isinstance(aggregates, list):
        titles = {}
        for document in aggregates:
            if isinstance(document, dict):
                doc_id = _text(document, "doc_id")
                if doc_id and _text(document, "doc_name"):
                    titles.setdefault(doc_id, document)
        aggregates = titles
    rows = []
    for chunk in data["chunks"][:limit]:
        if not isinstance(chunk, dict):
            return [], "KNOWLEDGE_RESULT_INVALID"
        content = chunk.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        dataset = _text(chunk, "dataset_id", "kb_id")
        document_id = _text(chunk, "document_id", "doc_id")
        title = _text(chunk, "document_keyword", "docnm_kwd")
        if not title and document_id and isinstance(aggregates, dict):
            document = aggregates.get(document_id)
            if isinstance(document, dict):
                title = _text(document, "doc_name")
        # Preserve existing keys for previously supported results. With no
        # document identity, keep unrelated snippets apart instead of claiming
        # they are all excerpts of the same unnamed document.
        identity = [dataset, document_id, title]
        if not document_id and not title:
            identity.append(_text(chunk, "id", "chunk_id") or content)
        document_key = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()
        highlight = chunk.get("highlight")
        terms = ([html.unescape(term) for term in re.findall(r"<em>([^<>]{1,80})</em>", highlight[:20000], re.I)]
                 if isinstance(highlight, str) else [])
        rows.append({"document_id": document_key, "chunk_id": _text(chunk, "id", "chunk_id")[:256],
                     "title": title or "知识库文档", "content": content, "highlights": terms})
    return rows, ""
