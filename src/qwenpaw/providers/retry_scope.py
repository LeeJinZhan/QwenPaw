"""Request-local retry ownership; never change a shared model's configuration."""
from contextvars import ContextVar

external_retry_owner: ContextVar[bool] = ContextVar('external_retry_owner', default=False)
