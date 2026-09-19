"""Request-scoped Runtime file and physical sandbox integration."""

from .scope import SandboxRequestScope, SandboxScopeError
from .file_refs import (
    FileRefError,
    FileRefRegistry,
    ResolvedTaskFile,
    get_file_ref_registry,
)

__all__ = [
    "FileRefError",
    "FileRefRegistry",
    "ResolvedTaskFile",
    "SandboxRequestScope",
    "SandboxScopeError",
    "get_file_ref_registry",
]
