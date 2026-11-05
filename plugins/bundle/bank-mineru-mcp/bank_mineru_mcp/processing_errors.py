"""Stable processing failures without leaking paths or operating-system messages."""
import errno


def processing_error_code(exc):
    if isinstance(exc, MemoryError) or (isinstance(exc, OSError) and exc.errno in {errno.ENOSPC, errno.EDQUOT, errno.ENOMEM}):
        return 'DOCUMENT_RESOURCE_EXHAUSTED'
    if isinstance(exc, (FileNotFoundError, PermissionError)):
        return 'DOCUMENT_SOURCE_UNAVAILABLE'
    if isinstance(exc, OSError):
        return 'DOCUMENT_ENGINE_UNAVAILABLE'
    return 'DOCUMENT_PARSE_FAILED'
