"""Publish complete permitted thought segments, never partial internal names.

Only the public process channel is filtered; answer prose and technical questions
are not keyword-rewritten. Rejected segments have no invented replacement.
"""
import re
from typing import Any

_INTERNAL = re.compile(
    r"runtime|qwenpaw|gateway|\bmcp\b|tool[ _-]?call|preflight|permit|"
    r"artifact[_ .](?:generate|revise|convert|job)|execute_shell|"
    r"\b(?:browser|shell|Skill)\b|\b[a-zA-Z][\w]*__[\w]+\b|"
    r"\b(?:task|gfile|trace|call|file|document_ref)_[\w-]+|"
    r"\b[a-z]+_[a-z_]+\b|\b[A-Z]+_[A-Z_]+\b|"
    r"工具(?:调用|网关|名称)|系统提示词|内部(?:路径|配置)|"
    r"https?://|file://|/(?:Users|etc|data|tmp|var)/|"
    r"token|password|secret|authorization|api[_ -]?key",
    re.IGNORECASE,
)
_SEGMENT_END = re.compile(r"[\n。！？]")


class PublicThinkingStream:
    def __init__(self) -> None:
        self.pending = ""
        self.dropping = False

    def project(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        if event.get("event") != "answer.thinking":
            result = self._finish() if event.get("event") in {
                "answer.chunk", "answer.completed", "answer.failed",
            } else []
            return result + [event]
        result = []
        text = str(event.get("text") or "")
        start = 0
        for match in _SEGMENT_END.finditer(text):
            self._append(text[start:match.end()])
            result.extend(self._finish())
            start = match.end()
        self._append(text[start:])
        return result

    def _append(self, value: str) -> None:
        if self.dropping:
            return
        if len(self.pending) + len(value) > 8192:
            self.pending = ""
            self.dropping = True
        else:
            self.pending += value

    def _finish(self) -> list[dict[str, Any]]:
        text, dropping = self.pending, self.dropping
        self.pending, self.dropping = "", False
        if dropping or not text.strip() or _INTERNAL.search(text):
            return []
        return [{"event": "answer.thinking", "text": text}]
