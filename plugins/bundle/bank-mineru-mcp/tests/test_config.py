from __future__ import annotations

from pathlib import Path

import pytest

from bank_mineru_mcp.config import MinerUConfigError, MinerUSettings


def _environment(monkeypatch, tmp_path: Path) -> Path:
    token_file = tmp_path / "mineru.token"
    token_file.write_text("secret-value\n", encoding="utf-8")
    token_file.chmod(0o600)
    values = {
        "BANK_MINERU_BASE_URL": "http://mineru.internal:8000",
        "BANK_MINERU_SUBMIT_MODE": "file_parse",
        "BANK_MINERU_TOKEN_FILE": str(token_file),
        "BANK_MINERU_MCP_HOST": "127.0.0.1",
        "BANK_MINERU_MCP_PORT": "18081",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return token_file


def test_settings_load_bounded_environment_and_redact_token(
    monkeypatch, tmp_path
) -> None:
    _environment(monkeypatch, tmp_path)

    settings = MinerUSettings.from_environment()

    assert settings.base_url == "http://mineru.internal:8000"
    assert settings.submit_mode == "file_parse"
    assert settings.token == "secret-value"
    assert settings.temp_ttl_seconds == 604800
    assert "secret-value" not in repr(settings)


def test_official_flash_settings_need_neither_token_nor_explicit_base_url(
    monkeypatch,
) -> None:
    for key in (
        "BANK_MINERU_BASE_URL",
        "BANK_MINERU_SUBMIT_MODE",
        "BANK_MINERU_TOKEN_FILE",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("BANK_MINERU_PROVIDER", "official_flash")

    settings = MinerUSettings.from_environment()

    assert settings.provider == "official_flash"
    assert settings.base_url == "https://mineru.net/api/v1/agent"
    assert settings.token == ""


def test_official_flash_settings_accept_dedicated_http_proxy(monkeypatch) -> None:
    for key in (
        "BANK_MINERU_BASE_URL",
        "BANK_MINERU_SUBMIT_MODE",
        "BANK_MINERU_TOKEN_FILE",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("BANK_MINERU_PROVIDER", "official_flash")
    monkeypatch.setenv("BANK_MINERU_PROXY_URL", "http://127.0.0.1:7897")

    settings = MinerUSettings.from_environment()

    assert settings.proxy_url == "http://127.0.0.1:7897"


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("BANK_MINERU_BASE_URL", "http://user:pass@mineru:8000", "URL"),
        ("BANK_MINERU_PROVIDER", "automatic", "provider"),
        ("BANK_MINERU_PROXY_URL", "http://user:pass@proxy:7897", "proxy"),
        ("BANK_MINERU_SUBMIT_MODE", "fallback", "mode"),
        ("BANK_MINERU_MCP_HOST", "0.0.0.0", "loopback"),
        ("BANK_MINERU_MCP_PORT", "70000", "port"),
        ("BANK_MINERU_PARSE_TIMEOUT_SECONDS", "0", "timeout"),
        ("BANK_MINERU_TEMP_TTL_SECONDS", "604801", "TTL"),
    ],
)
def test_settings_reject_unsafe_or_unbounded_values(
    monkeypatch,
    tmp_path,
    key,
    value,
    message,
) -> None:
    _environment(monkeypatch, tmp_path)
    monkeypatch.setenv(key, value)

    with pytest.raises(MinerUConfigError, match=message):
        MinerUSettings.from_environment()


def test_settings_reject_missing_or_writable_token_file(monkeypatch, tmp_path) -> None:
    token_file = _environment(monkeypatch, tmp_path)
    token_file.chmod(0o622)
    with pytest.raises(MinerUConfigError, match="token"):
        MinerUSettings.from_environment()

    token_file.unlink()
    with pytest.raises(MinerUConfigError, match="token"):
        MinerUSettings.from_environment()
