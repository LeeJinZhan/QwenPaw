"""Configuration loader for Ops Observer.

Reads values from a ``.env`` file in the plugin directory first, then
falls back to OS environment variables. ``.env`` takes precedence so
operators can keep a self-contained config next to the plugin without
polluting the host environment.

Supported keys (all optional):

  OPS_OBSERVER_DB_URL      MySQL connection URL. When unset or empty,
                           the plugin uses the default SQLite backend.
                           Example: mysql://user:pass@host:3306/dbname

  QWENPAW_WORKING_DIR      Root working directory. SQLite database and
                           run-summary JSON files are stored here.
                           Default: ~/.qwenpaw

  QWENPAW_SECRET_DIR       Directory for secrets (event token).
                           Default: <QWENPAW_WORKING_DIR>.secret
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

try:
    from dotenv import dotenv_values
except ImportError:  # python-dotenv not installed yet (e.g. during tests)
    dotenv_values = None  # type: ignore[assignment]


_PLUGIN_DIR = Path(__file__).resolve().parent
_ENV_FILE = _PLUGIN_DIR / ".env"


@lru_cache(maxsize=1)
def _load_env() -> dict[str, str | None]:
    """Load .env once, merging with os.environ.

    Priority: .env file > existing os.environ (for keys present in .env).
    Keys absent from .env fall through to os.environ at access time.
    """
    if dotenv_values is None or not _ENV_FILE.exists():
        return {}
    return dotenv_values(_ENV_FILE)


def _get(key: str, default: str | None = None) -> str | None:
    """Return config value: .env file first, then os.environ, then default."""
    env_values = _load_env()
    if key in env_values and env_values[key] is not None:
        return env_values[key]
    return os.environ.get(key, default)


def get_db_url() -> str:
    """Return the database URL string (empty = use SQLite)."""
    return (_get("OPS_OBSERVER_DB_URL") or "").strip()


def get_working_dir() -> Path:
    """Return the resolved working directory path."""
    return Path(_get("QWENPAW_WORKING_DIR") or "~/.qwenpaw").expanduser().resolve()


def get_secret_dir(working_dir: Path) -> Path:
    """Return the resolved secret directory path."""
    return Path(_get("QWENPAW_SECRET_DIR") or (str(working_dir) + ".secret")).expanduser().resolve()


def env_file_path() -> Path:
    """Return the path to the .env file (for documentation / UI display)."""
    return _ENV_FILE
