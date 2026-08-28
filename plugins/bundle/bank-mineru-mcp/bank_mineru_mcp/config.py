"""Fail-closed environment and secret-file configuration."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import stat
from urllib.parse import urlsplit, urlunsplit


class MinerUConfigError(RuntimeError):
    """MinerU plugin configuration is missing or unsafe."""


@dataclass(frozen=True)
class MinerUSettings:
    base_url: str
    submit_mode: str
    token: str = field(repr=False)
    provider: str = "self_hosted"
    proxy_url: str = ""
    connect_timeout_seconds: float = 5
    upload_timeout_seconds: float = 120
    parse_timeout_seconds: float = 900
    poll_interval_seconds: float = 1
    mcp_host: str = "127.0.0.1"
    mcp_port: int = 18081
    inline_max_chars: int = 20_000
    result_max_bytes: int = 32 * 1024 * 1024
    task_result_max_bytes: int = 64 * 1024 * 1024
    temp_ttl_seconds: int = 604_800

    @classmethod
    def from_environment(cls) -> "MinerUSettings":
        provider = (
            str(os.environ.get("BANK_MINERU_PROVIDER", "self_hosted")).strip().lower()
        )
        if provider not in {"self_hosted", "official_flash"}:
            raise MinerUConfigError(
                "MinerU provider must be self_hosted or official_flash"
            )
        default_base_url = (
            "https://mineru.net/api/v1/agent" if provider == "official_flash" else ""
        )
        base_url = _base_url(
            os.environ.get("BANK_MINERU_BASE_URL", "") or default_base_url
        )
        submit_mode = str(os.environ.get("BANK_MINERU_SUBMIT_MODE", "tasks")).strip()
        if provider == "self_hosted" and submit_mode not in {"tasks", "file_parse"}:
            raise MinerUConfigError("MinerU submit mode must be tasks or file_parse")
        host = str(os.environ.get("BANK_MINERU_MCP_HOST", "127.0.0.1")).strip()
        if host not in {"127.0.0.1", "::1", "localhost"}:
            raise MinerUConfigError("MinerU MCP host must be loopback")
        port = _integer("BANK_MINERU_MCP_PORT", 18081, 1, 65535, "port")
        connect = _number("BANK_MINERU_CONNECT_TIMEOUT_SECONDS", 5, 0.1, 60, "timeout")
        upload = _number("BANK_MINERU_UPLOAD_TIMEOUT_SECONDS", 120, 1, 600, "timeout")
        parse = _number("BANK_MINERU_PARSE_TIMEOUT_SECONDS", 900, 1, 3600, "timeout")
        poll = _number("BANK_MINERU_POLL_INTERVAL_SECONDS", 1, 0.1, 10, "poll interval")
        inline = _integer(
            "BANK_MINERU_INLINE_MAX_CHARS", 20_000, 1_000, 100_000, "inline limit"
        )
        result_max = _integer(
            "BANK_MINERU_RESULT_MAX_BYTES",
            32 * 1024 * 1024,
            1024,
            64 * 1024 * 1024,
            "result limit",
        )
        task_max = _integer(
            "BANK_MINERU_TASK_RESULT_MAX_BYTES",
            64 * 1024 * 1024,
            result_max,
            256 * 1024 * 1024,
            "task result limit",
        )
        ttl = _integer(
            "BANK_MINERU_TEMP_TTL_SECONDS",
            604_800,
            60,
            604_800,
            "TTL",
        )
        return cls(
            base_url=base_url,
            submit_mode=(
                submit_mode if provider == "self_hosted" else "official_flash"
            ),
            token=(
                _read_token(os.environ.get("BANK_MINERU_TOKEN_FILE", ""))
                if provider == "self_hosted"
                else ""
            ),
            provider=provider,
            proxy_url=_proxy_url(os.environ.get("BANK_MINERU_PROXY_URL", "")),
            connect_timeout_seconds=connect,
            upload_timeout_seconds=upload,
            parse_timeout_seconds=parse,
            poll_interval_seconds=poll,
            mcp_host=host,
            mcp_port=port,
            inline_max_chars=inline,
            result_max_bytes=result_max,
            task_result_max_bytes=task_max,
            temp_ttl_seconds=ttl,
        )


def _base_url(value: str) -> str:
    normalized = str(value or "").strip().rstrip("/")
    parsed = urlsplit(normalized)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise MinerUConfigError("MinerU base URL is invalid")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


def _proxy_url(value: str) -> str:
    normalized = str(value or "").strip().rstrip("/")
    if not normalized:
        return ""
    parsed = urlsplit(normalized)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise MinerUConfigError("MinerU proxy URL is invalid")
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def _read_token(value: str) -> str:
    token_path = Path(str(value or "").strip()).expanduser()
    try:
        metadata = token_path.lstat()
    except OSError as exc:
        raise MinerUConfigError("MinerU token file is unavailable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise MinerUConfigError("MinerU token file must be a regular file")
    if metadata.st_mode & 0o022:
        raise MinerUConfigError("MinerU token file is writable by other users")
    try:
        token = token_path.read_text(encoding="utf-8").removesuffix("\n")
    except (OSError, UnicodeError) as exc:
        raise MinerUConfigError("MinerU token file is unreadable") from exc
    if not token or "\n" in token or "\r" in token or len(token) > 8192:
        raise MinerUConfigError("MinerU token file content is invalid")
    return token


def _number(
    name: str,
    default: float,
    minimum: float,
    maximum: float,
    label: str,
) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError) as exc:
        raise MinerUConfigError(f"MinerU {label} is invalid") from exc
    if not minimum <= value <= maximum:
        raise MinerUConfigError(f"MinerU {label} is outside its allowed range")
    return value


def _integer(
    name: str,
    default: int,
    minimum: int,
    maximum: int,
    label: str,
) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError) as exc:
        raise MinerUConfigError(f"MinerU {label} is invalid") from exc
    if not minimum <= value <= maximum:
        raise MinerUConfigError(f"MinerU {label} is outside its allowed range")
    return value


__all__ = ["MinerUConfigError", "MinerUSettings"]
