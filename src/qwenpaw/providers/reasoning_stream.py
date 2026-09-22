"""Classify inline reasoning before AgentScope creates text/history blocks."""
from __future__ import annotations

import re

from qwenpaw.exceptions import ModelExecutionException

_MARKERS = ("<think>", "</think>")
_TOKEN = re.compile(r"</?think>|`+", re.IGNORECASE)


def invalid_reasoning_stream(reason: str) -> ModelExecutionException:
    return ModelExecutionException(model="upstream", details={"stream_error": reason})


class ReasoningTextStream:
    """A per-call parser; buffers only undecided content and partial markers.

    Some reasoning endpoints omit the opening tag. In that compatibility mode,
    text without a structured reasoning field stays private until a closing tag
    or a verified normal finish. Structured reasoning and ordinary text models
    keep their incremental first-token behavior. Code-quoted tags are literal.
    """

    def __init__(self, *, buffer_unclassified: bool = False) -> None:
        self._state = "prefix"
        self._guard = buffer_unclassified
        self._pending = ""
        self._undecided: list[str] = []
        self._undecided_size = 0
        self._code_ticks = 0
        self._answer_started = False

    def feed(self, text: str, *, structured_reasoning: bool = False) -> tuple[str, str]:
        if structured_reasoning and self._state in {"prefix", "unknown"}:
            if self._undecided_size:
                raise invalid_reasoning_stream("reasoning_after_unclassified_content")
            self._guard = False
        answer: list[str] = []
        thinking: list[str] = []
        self._pending += text
        while self._pending:
            match = _TOKEN.search(self._pending)
            if match is None:
                keep = 0
                if not self._code_ticks:
                    lower = self._pending.lower()
                    for size in range(1, min(len(lower), len("</think>") - 1) + 1):
                        if any(marker.startswith(lower[-size:]) for marker in _MARKERS):
                            keep = size
                safe = self._pending[:-keep] if keep else self._pending
                self._pending = self._pending[-keep:] if keep else ""
                self._emit(safe, answer, thinking)
                break
            self._emit(self._pending[:match.start()], answer, thinking)
            token = match.group()
            # Keep a trailing tick run until its complete delimiter is known.
            if token.startswith("`") and match.end() == len(self._pending):
                self._pending = self._pending[match.start():]
                break
            self._pending = self._pending[match.end():]
            if token.startswith("`"):
                if not self._code_ticks:
                    self._code_ticks = len(token)
                elif self._code_ticks == len(token):
                    self._code_ticks = 0
                self._emit(token, answer, thinking)
            elif self._code_ticks:
                self._emit(token, answer, thinking)
            elif token.lower() == "<think>":
                if self._state == "thinking":
                    raise invalid_reasoning_stream("nested_thinking_tag")
                thinking.extend(self._take_undecided())
                self._state = "thinking"
            else:
                if self._state not in {"thinking", "unknown", "prefix"} and self._answer_started:
                    raise invalid_reasoning_stream("unexpected_thinking_end")
                thinking.extend(self._take_undecided())
                self._state = "text"
        return "".join(answer), "".join(thinking)

    def finish(self) -> tuple[str, str]:
        if self._state == "thinking":
            raise invalid_reasoning_stream("unclosed_thinking_tag")
        if self._pending and not self._pending.startswith("`") and self._pending not in {"<", "</"}:
            raise invalid_reasoning_stream("incomplete_thinking_tag")
        answer: list[str] = []
        thinking: list[str] = []
        self._emit(self._pending, answer, thinking)
        self._pending = ""
        answer.extend(self._take_undecided())
        return "".join(answer), "".join(thinking)

    def _take_undecided(self) -> list[str]:
        result = self._undecided
        self._undecided = []
        self._undecided_size = 0
        return result

    def _emit(self, text: str, answer: list[str], thinking: list[str]) -> None:
        if not text:
            return
        if self._state == "prefix":
            self._state = "unknown" if self._guard else "text"
        if self._state == "unknown":
            self._undecided_size += len(text)
            if self._undecided_size > 1_000_000:
                raise invalid_reasoning_stream("unclassified_content_limit")
            self._undecided.append(text)
        elif self._state == "thinking":
            thinking.append(text)
        else:
            answer.append(text)
            self._answer_started = self._answer_started or bool(text.strip())
