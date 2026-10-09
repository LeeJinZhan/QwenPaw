"""Read-only
evidence of client wire settings, never supplier defaults or revisions.
"""
from __future__ import annotations

import hashlib
import json
import math
from importlib.metadata import version
from urllib.parse import urlsplit


_GENERATION_FIELDS = frozenset(
    {
        "temperature",
        "top_p",
        "seed",
        "max_tokens",
        "max_completion_tokens",
        "frequency_penalty",
        "presence_penalty",
        "repetition_penalty",
        "stop",
        "reasoning_effort",
        "extra_body",
        "response_format",
        "logprobs",
        "top_logprobs",
        "n",
        "parallel_tool_calls",
        "audio",
        "modalities",
    }
)


def _bounded_json(value, depth=0):
    if depth > 8:
        raise ValueError("CLASSIFICATION_REQUEST_METADATA_INVALID")
    if value is None or type(value) is bool:
        return
    if type(value) in (int, float):
        if not math.isfinite(value):
            raise ValueError("CLASSIFICATION_REQUEST_METADATA_INVALID")
        return
    if isinstance(value, str) and len(value) <= 1024:
        return
    if isinstance(value, list) and len(value) <= 100:
        for item in value:
            _bounded_json(item, depth + 1)
        return
    if isinstance(value, dict) and len(value) <= 100:
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > 128:
                raise ValueError("CLASSIFICATION_REQUEST_METADATA_INVALID")
            _bounded_json(item, depth + 1)
        return
    raise ValueError("CLASSIFICATION_REQUEST_METADATA_INVALID")


def observe_openai_chat_request(model, model_name, overrides):
    """Mirror the pinned SDK's final merge without constructing an API client.

    Missing keys mean absent wire fields, not known server defaults. Headers
    are not returned. Endpoint overrides are read from actual client kwargs.
    """
    if model_name != model.model or set(overrides) - _GENERATION_FIELDS - {
        "extra_headers",
        "timeout",
    }:
        raise ValueError("CLASSIFICATION_REQUEST_OVERRIDE_REJECTED")
    if version("agentscope") != "2.0.4.post1":
        raise ValueError("CLASSIFICATION_REQUEST_SDK_UNSUPPORTED")
    endpoint = model.client_kwargs.get("base_url", model.credential.base_url)
    parsed = urlsplit(str(endpoint))
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("CLASSIFICATION_REQUEST_ENDPOINT_INVALID")
    from .chat_message_order import validate_extra_body

    validate_extra_body(model.extra_body)
    validate_extra_body(overrides.get("extra_body"))
    headers = dict(model.client_kwargs.get("default_headers") or {})
    headers.update(getattr(model, "_default_headers", None) or {})
    headers.update(overrides.get("extra_headers") or {})
    if any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in headers.items()
    ):
        raise ValueError("CLASSIFICATION_REQUEST_HEADERS_INVALID")
    if set(model.client_kwargs) & {"api_key", "organization"}:
        raise ValueError("CLASSIFICATION_REQUEST_CREDENTIAL_OVERRIDE_REJECTED")
    parameters = model.parameters
    wire = {}
    if parameters.max_tokens is not None:
        wire["max_completion_tokens"] = parameters.max_tokens
    for name in ("temperature", "top_p"):
        value = getattr(parameters, name)
        if value is not None:
            wire[name] = value
    if parameters.thinking_enable and parameters.reasoning_effort:
        wire["reasoning_effort"] = parameters.reasoning_effort
    if parameters.voice is not None:
        wire.update(
            audio={"voice": parameters.voice, "format": "pcm16"},
            modalities=["text", "audio"],
        )
    if model.extra_body is not None:
        wire["extra_body"] = dict(model.extra_body)
    wire.update(
        {
            key: value
            for key, value in overrides.items()
            if key in _GENERATION_FIELDS
        }
    )
    # OpenAI's transport applies extra_body after standard JSON fields.
    extra_body = wire.pop("extra_body", None)
    if extra_body is not None:
        wire.update(extra_body)
    if not parameters.parallel_tool_calls:
        wire["parallel_tool_calls"] = False
    _bounded_json(wire)
    if len(json.dumps(wire, sort_keys=True).encode()) > 8192:
        raise ValueError("CLASSIFICATION_REQUEST_METADATA_INVALID")
    return {
        "schema_version": "qwenpaw-client-request-1",
        "sdk_version": version("agentscope"),
        "provider_id": model.qwenpaw_provider_id,
        "model_id": model_name,
        "endpoint_sha256": hashlib.sha256(
            str(endpoint).rstrip("/").encode()
        ).hexdigest(),
        "stream": model.stream,
        "wire_parameters": wire,
        "headers_sha256": hashlib.sha256(
            json.dumps(headers, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "model_revision": None,
        "gateway_revision": None,
    }
