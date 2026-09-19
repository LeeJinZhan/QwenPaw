"""Lifecycle hooks for the Bank Runtime plugin."""

from .production_guard import execute_production_guard


def bank_runtime_startup_guard() -> None:
    """Evaluate the strict production surface after all registrations."""
    execute_production_guard()
