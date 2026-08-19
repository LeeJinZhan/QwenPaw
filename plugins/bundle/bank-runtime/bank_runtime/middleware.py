"""Fail-closed middleware extension point for later Gateway integration."""

from __future__ import annotations

from typing import Any


def bank_runtime_middleware_factory(
    context: Any,
    agent_config: Any,
) -> None:
    """Do not install behavior until Task 8 supplies Gateway mediation."""
    return None
