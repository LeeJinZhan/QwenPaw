"""Compatibility import for the Task 8 Gateway middleware package."""

from .gateway.middleware import bank_runtime_middleware_factory

__all__ = ["bank_runtime_middleware_factory"]
