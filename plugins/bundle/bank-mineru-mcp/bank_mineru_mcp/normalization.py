"""Normalize the small approved subset of MinerU JSON into bounded documents."""

from __future__ import annotations

import re
from typing import Any, Mapping

from .schemas import NormalizedChunk, NormalizedDocument

_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*$")


def normalize_mineru_result(
    payload: Any,
    *,
    upload_stems: Mapping[str, str],
    chunk_chars: int = 4000,
) -> dict[str, NormalizedDocument]:
    results = payload.get("results") if isinstance(payload, Mapping) else None
    if not isinstance(results, Mapping):
        results = {}
    normalized: dict[str, NormalizedDocument] = {}
    for file_id, stem in upload_stems.items():
        raw = results.get(stem)
        markdown = raw.get("md_content") if isinstance(raw, Mapping) else None
        if not isinstance(markdown, str) or not markdown.strip():
            normalized[file_id] = NormalizedDocument(
                title="",
                markdown="",
                chunks=(),
                error_code="MINERU_PARSE_FAILED",
            )
            continue
        page_count = _positive_int(raw.get("page_count"))
        normalized[file_id] = NormalizedDocument(
            title=_title(markdown),
            markdown=markdown,
            chunks=_chunks(markdown, max(1, int(chunk_chars))),
            page_count=page_count,
        )
    return normalized


def _title(markdown: str) -> str:
    for line in markdown.splitlines():
        match = _HEADING.match(line)
        if match:
            return match.group(1)[:300]
    return ""


def _chunks(markdown: str, limit: int) -> tuple[NormalizedChunk, ...]:
    chunks: list[NormalizedChunk] = []
    heading = ""
    position = 0
    while position < len(markdown):
        end = min(position + limit, len(markdown))
        if end < len(markdown):
            newline = markdown.rfind("\n", position + max(1, limit // 2), end)
            if newline > position:
                end = newline + 1
        text = markdown[position:end]
        for line in text.splitlines():
            match = _HEADING.match(line)
            if match:
                heading = match.group(1)[:300]
                break
        chunks.append(NormalizedChunk(index=len(chunks), heading=heading, text=text))
        position = end
    return tuple(chunks)


def _positive_int(value: Any) -> int | None:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return None
    return normalized if normalized >= 0 else None


__all__ = ["normalize_mineru_result"]
