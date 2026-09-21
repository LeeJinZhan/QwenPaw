"""Normalize system instructions at the final OpenAI-compatible wire boundary."""
from copy import deepcopy
from collections.abc import Mapping
from typing import Any


def validate_extra_body(extra_body: Any) -> None:
    """Keep SDK extensions from replacing framework-owned request structure."""
    if extra_body is None:
        return
    if not isinstance(extra_body, Mapping):
        raise ValueError("extra_body must be an object")
    reserved = {"messages", "model", "stream", "tools", "tool_choice"}
    conflicts = reserved.intersection(extra_body)
    if conflicts:
        raise ValueError(
            "extra_body cannot override managed request fields: "
            + ", ".join(sorted(conflicts))
        )


class SystemMessageOrderFormatter:
    """Normalize wire dictionaries after the configured formatter has run.

    Used only on a request-local model view. The provider's real formatter
    remains available to capability discovery, media handling and retries.
    """

    def __init__(self, formatter: Any) -> None:
        self._formatter = formatter

    async def format(self, messages: Any) -> Any:
        return normalize_system_messages(await self._formatter.format(messages))


def normalize_system_messages(messages: Any) -> Any:
    """One leading system message; conversation and tool pairs stay in order.

    Only actual system roles are moved. User/tool content is never promoted.
    Block metadata is retained, and input/history objects are not mutated.
    """
    if not isinstance(messages, (list, tuple)):
        return messages
    systems = [m for m in messages if isinstance(m, dict) and m.get("role") == "system"]
    if not systems or (len(systems) == 1 and messages[0] is systems[0]):
        return messages
    metadata: dict[str, Any] = {}
    contents = []
    for message in systems:
        for key, value in message.items():
            if key in {"role", "content"}:
                continue
            if key in metadata and metadata[key] != value:
                raise ValueError("Conflicting system message metadata cannot be merged")
            metadata[key] = deepcopy(value)
        content = message.get("content")
        if not isinstance(content, (str, list)):
            raise ValueError("System message content must be text or content blocks")
        contents.append(content)
    if all(isinstance(content, str) for content in contents):
        combined = "\n\n".join(contents)
    else:
        combined = []
        for content in contents:
            combined.extend([{"type": "text", "text": content}]
                            if isinstance(content, str) else deepcopy(content))
    system = {**metadata, "role": "system", "content": combined}
    return [system, *(m for m in messages
                      if not (isinstance(m, dict) and m.get("role") == "system"))]
