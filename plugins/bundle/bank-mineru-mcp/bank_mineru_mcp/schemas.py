"""Internal normalized models which never expose MinerU raw output."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class NormalizedChunk:
    index: int
    text: str
    heading: str = ""
    page_start: int | None = None
    page_end: int | None = None


@dataclass(frozen=True)
class NormalizedDocument:
    title: str
    markdown: str
    chunks: tuple[NormalizedChunk, ...]
    page_count: int | None = None
    error_code: str | None = None


@dataclass(frozen=True)
class DocumentHandle:
    document_ref: str
    path: Path
    title: str
    page_count: int | None
    chunk_count: int


@dataclass(frozen=True)
class ChunkPage:
    document_ref: str
    chunks: tuple[NormalizedChunk, ...]
    next_cursor: str | None
    has_more: bool


__all__ = [
    "ChunkPage",
    "DocumentHandle",
    "NormalizedChunk",
    "NormalizedDocument",
]
