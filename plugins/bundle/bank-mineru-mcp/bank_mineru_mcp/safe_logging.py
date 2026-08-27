"""Helpers for correlating MinerU operations without logging sensitive values."""

from __future__ import annotations

import hashlib


def identifier_digest(value: str) -> str:
    """Return a short irreversible correlation label."""
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]


__all__ = ["identifier_digest"]
