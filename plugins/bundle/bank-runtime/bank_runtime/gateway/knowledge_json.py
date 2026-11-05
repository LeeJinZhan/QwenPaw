"""Decode citation and error fields; scan unused metadata without decoding it.

Ignored string values need a clear quoted boundary, but their escapes/control
characters are not interpreted. Object/array structure, required fields and
all error indicators remain strict. This is not a repair of arbitrary JSON.
"""
from __future__ import annotations

import json
import re
from typing import Any

_FIELDS = {
    "business": {
        **dict.fromkeys(("schema_version", "status", "sources", "code", "error", "error_code", "error_codes", "isError", "type", "text"), "full"),
        "chunks": "chunks", "data": "business", "structuredContent": "business",
        "content": "business", "department_scope": "scope", "doc_aggs": "aggregates",
    },
    "scope": {"error": "full"},
    "chunk": dict.fromkeys(("content", "dataset_id", "kb_id", "document_id", "doc_id",
                           "document_keyword", "docnm_kwd", "id", "chunk_id", "highlight"), "full"),
    "aggregate": {"doc_id": "full", "doc_name": "full"},
}
_ARRAY_ITEMS = {"business": "business", "chunks": "chunk", "aggregates": "aggregate"}
_STRING_BOUNDARY = re.compile(r'["\\]')
_NUMBER = re.compile(r'-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\Z')
_SCALARS = frozenset(("true", "false", "null", "NaN", "Infinity", "-Infinity"))


def decode_retrieval_json(text: str) -> Any:
    """Keep the existing Python JSON scalar semantics for selected values."""
    parser = _RetrievalDecoder(text)
    value = parser.value("business", 0)
    parser.whitespace()
    if parser.position != len(text):
        parser.fail("Extra data")
    return value


class _RetrievalDecoder:
    def __init__(self, text: str):
        self.text = text
        self.position = 0
        self.decoder = json.JSONDecoder()

    def fail(self, message: str) -> None:
        raise json.JSONDecodeError(message, self.text, self.position)

    def whitespace(self) -> None:
        while self.position < len(self.text) and self.text[self.position] in " \t\n\r":
            self.position += 1

    def current(self) -> str:
        self.whitespace()
        return self.text[self.position:self.position + 1]

    def value(self, mode: str | None, depth: int) -> Any:
        if depth > 64:
            self.fail("Retrieval JSON nesting limit exceeded")
        char = self.current()
        if not char:
            self.fail("Expecting value")
        if mode is None:
            self.skip(depth)
            return None
        if char == "{" and (mode in _FIELDS or mode == "aggregates"):
            return self.object(mode, depth)
        if char == "[" and mode in _ARRAY_ITEMS:
            return self.array(_ARRAY_ITEMS[mode], depth)
        value, self.position = self.decoder.raw_decode(self.text, self.position)
        return value

    def object(self, mode: str | None, depth: int) -> dict:
        self.position += 1
        result = {}
        if self.current() == "}":
            self.position += 1
            return result
        while True:
            if self.current() != '"':
                self.fail("Expecting property name enclosed in double quotes")
            key, self.position = self.decoder.raw_decode(self.text, self.position)
            if self.current() != ":":
                self.fail("Expecting ':' delimiter")
            self.position += 1
            child = "aggregate" if mode == "aggregates" else _FIELDS.get(mode, {}).get(key)
            if child is not None and key in result:
                self.fail("Duplicate retrieval field")
            value = self.value(child, depth + 1)
            if child is not None:
                result[key] = value
            char = self.current()
            self.position += 1
            if char == "}":
                return result
            if char != ",":
                self.fail("Expecting ',' delimiter")

    def array(self, item_mode: str | None, depth: int) -> list:
        self.position += 1
        result = []
        if self.current() == "]":
            self.position += 1
            return result
        while True:
            value = self.value(item_mode, depth + 1)
            if item_mode is not None:
                result.append(value)
            char = self.current()
            self.position += 1
            if char == "]":
                return result
            if char != ",":
                self.fail("Expecting ',' delimiter")

    def skip(self, depth: int) -> None:
        char = self.current()
        if char == "{":
            self.object(None, depth)
        elif char == "[":
            self.array(None, depth)
        elif char == '"':
            self.position += 1
            while match := _STRING_BOUNDARY.search(self.text, self.position):
                self.position = match.end()
                if match.group() == '"':
                    return
                # An escaped character cannot terminate the quoted value.
                self.position += 1
            self.fail("Unterminated ignored string")
        else:
            start = self.position
            while self.position < len(self.text) and self.text[self.position] not in " \t\n\r,]}":
                self.position += 1
            token = self.text[start:self.position]
            if token not in _SCALARS and not _NUMBER.fullmatch(token):
                self.fail("Invalid ignored scalar")
