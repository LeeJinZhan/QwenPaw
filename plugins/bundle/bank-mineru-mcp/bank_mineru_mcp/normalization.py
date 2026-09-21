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
    """Structure-aware chunking: tables stay row-aligned and repeat headers."""
    chunks: list[NormalizedChunk] = []
    heading = ""
    for block in _blocks(markdown):
        if block["table"]:
            lines = block["text"].splitlines(keepends=True)
            header = lines[:2]
            rows = lines[2:]
            if not rows:
                chunks.append(_chunk_from_lines(chunks, heading, header))
                continue
            current: list[str] = []
            size = 0
            for row in rows:
                if current and size + len(row) > limit:
                    chunks.append(_chunk_from_lines(chunks, heading, header + current))
                    current, size = [], 0
                current.append(row)
                size += len(row)
            if current:
                chunks.append(_chunk_from_lines(chunks, heading, header + current))
            continue
        text_block = block["text"]
        position = 0
        while position < len(text_block):
            end = min(position + limit, len(text_block))
            if end < len(text_block):
                newline = text_block.rfind("\n", position + max(1, limit // 2), end)
                if newline > position:
                    end = newline + 1
            text = text_block[position:end]
            for line in text.splitlines():
                match = _HEADING.match(line)
                if match:
                    heading = match.group(1)[:300]
                    break
            chunks.append(NormalizedChunk(index=len(chunks), heading=heading, text=text))
            position = end
    return tuple(chunks)


def _table_cells(line: str) -> list[str]:
    return re.split(r"(?<!\\)\|", line.strip().strip("|"))


def _table_header(first: str, second: str) -> bool:
    if "|" not in first or "|" not in second:
        return False
    cells = _table_cells(second)
    return len(cells) == len(_table_cells(first)) and all(
        re.fullmatch(r"\s*:?-{3,}:?\s*", cell) for cell in cells
    )


def _blocks(markdown: str) -> list[dict[str, Any]]:
    lines = markdown.splitlines(keepends=True)
    blocks: list[dict[str, Any]] = []
    prose: list[str] = []
    fence = ""
    index = 0
    while index < len(lines):
        line = lines[index]
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if marker:
            token = marker.group(1)
            if not fence:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = ""
            prose.append(line)
            index += 1
            continue
        if not fence and index + 1 < len(lines) and _table_header(line, lines[index + 1]):
            if prose:
                blocks.append({"text": "".join(prose), "table": False})
                prose = []
            table = lines[index:index + 2]
            index += 2
            while index < len(lines) and lines[index].strip() and "|" in lines[index]:
                if re.match(r"^\s*(`{3,}|~{3,})", lines[index]):
                    break
                table.append(lines[index])
                index += 1
            blocks.append({"text": "".join(table), "table": True})
        else:
            prose.append(line)
            index += 1
    if prose:
        blocks.append({"text": "".join(prose), "table": False})
    return blocks


def _chunk_from_lines(
    chunks: list[NormalizedChunk], heading: str, lines: list[str]
) -> NormalizedChunk:
    for line in lines:
        match = _HEADING.match(line)
        if match:
            heading = match.group(1)[:300]
            break
    return NormalizedChunk(index=len(chunks), heading=heading, text="".join(lines))


def _positive_int(value: Any) -> int | None:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return None
    return normalized if normalized >= 0 else None


__all__ = ["normalize_mineru_result"]
