# -*- coding: utf-8 -*-
"""Resolve the Runtime endpoint reachable from the QwenPaw process."""
from __future__ import annotations

import os
from typing import Any, Mapping
from urllib.parse import urlparse, urlunparse


RUNTIME_BASE_URL_ENV_KEYS = (
    "QWENPAW_RUNTIME_BASE_URL",
    "BANK_RUNTIME_BASE_URL",
    "RUNTIME_BASE_URL",
)


def resolve_runtime_base_url(gateway: Mapping[str, Any] | None = None) -> str:
    """Prefer the deployment endpoint over request metadata.

    Runtime Tool Gateway metadata can contain an address that is valid from
    the Runtime host but not from the Worker container (for example,
    ``127.0.0.1``).  Explicit Worker environment configuration therefore has
    precedence for all Runtime callbacks.
    """
    for key in RUNTIME_BASE_URL_ENV_KEYS:
        value = os.environ.get(key, "").strip().rstrip("/")
        if value:
            return value
    if not isinstance(gateway, Mapping):
        return ""
    base_url = str(
        gateway.get("base_url") or gateway.get("runtime_base_url") or "",
    ).strip().rstrip("/")
    if base_url:
        return base_url
    endpoint = str(gateway.get("endpoint", "")).strip()
    parsed = urlparse(endpoint)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))
    return ""
