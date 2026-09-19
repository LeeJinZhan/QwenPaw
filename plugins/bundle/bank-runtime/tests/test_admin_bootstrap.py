import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bank_runtime.admin_bootstrap import (
    provision_administrator,
    verify_administrator,
    main,
)


@pytest.fixture
def secret_dir(tmp_path):
    path = tmp_path.resolve() / "secrets"
    path.mkdir(mode=0o700)
    return path


@pytest.fixture
def native_auth(secret_dir, monkeypatch):
    from qwenpaw.app import auth
    from qwenpaw.security import secret_store

    monkeypatch.setattr(auth, "AUTH_FILE", secret_dir / "auth.json")
    monkeypatch.setattr(secret_store, "_get_secret_dir", lambda: secret_dir)
    monkeypatch.setattr(secret_store, "_cached_master_key", None)
    monkeypatch.setattr(secret_store, "_cached_fernet", None)
    monkeypatch.setenv("QWENPAW_DISABLE_KEYRING", "true")
    return auth


def test_native_login_uses_offline_account_and_retry_preserves_it(
    secret_dir, native_auth
):
    assert (
        provision_administrator(secret_dir, "admin", "password-12345", "password-12345")
        == "created"
    )
    assert verify_administrator(secret_dir) == "admin"
    before = (secret_dir / "auth.json").read_bytes()
    assert b"password-12345" not in before
    assert native_auth.authenticate("admin", "password-12345")
    assert native_auth.authenticate("admin", "incorrect") is None
    assert (
        provision_administrator(
            secret_dir, "admin", "different-password", "different-password"
        )
        == "already_exists"
    )
    assert (secret_dir / "auth.json").read_bytes() == before
    for name in ("auth.json", ".master_key"):
        assert (secret_dir / name).stat().st_mode & 0o777 == 0o600


def test_reset_rotates_native_sessions_but_keeps_encryption_key(
    secret_dir, native_auth
):
    provision_administrator(secret_dir, "admin", "password-12345", "password-12345")
    token = native_auth.authenticate("admin", "password-12345")
    key = (secret_dir / ".master_key").read_bytes()
    assert (
        provision_administrator(
            secret_dir, "admin", "new-password-12345", "new-password-12345", reset=True
        )
        == "reset"
    )
    assert native_auth.verify_token(token) is None
    assert native_auth.authenticate("admin", "password-12345") is None
    assert native_auth.authenticate("admin", "new-password-12345")
    assert (secret_dir / ".master_key").read_bytes() == key


@pytest.mark.parametrize(
    "damage",
    ["key_missing", "wrong_key", "broken_auth", "plaintext_jwt", "username_conflict"],
)
def test_existing_bad_auth_is_not_overwritten(secret_dir, damage):
    provision_administrator(secret_dir, "admin", "password-12345", "password-12345")
    auth = secret_dir / "auth.json"
    if damage == "key_missing":
        (secret_dir / ".master_key").unlink()
    if damage == "wrong_key":
        (secret_dir / ".master_key").write_text("11" * 32)
    if damage == "broken_auth":
        auth.write_text("{")
    if damage == "plaintext_jwt":
        data = json.loads(auth.read_text())
        data["jwt_secret"] = "plain-secret"
        auth.write_text(json.dumps(data))
    before = auth.read_bytes()
    with pytest.raises(Exception):
        provision_administrator(
            secret_dir,
            "different" if damage == "username_conflict" else "admin",
            "password-12345",
            "password-12345",
        )
    assert auth.read_bytes() == before


def test_mismatched_password_or_existing_secret_without_key_creates_no_auth(secret_dir):
    with pytest.raises(ValueError):
        provision_administrator(secret_dir, "admin", "password-12345", "mismatch")
    assert list(secret_dir.iterdir()) == []
    (secret_dir / "providers.json").write_text("existing provider credentials")
    with pytest.raises(ValueError):
        provision_administrator(secret_dir, "admin", "password-12345", "password-12345")
    assert not (secret_dir / "auth.json").exists()
    assert not (secret_dir / ".master_key").exists()


def test_cli_redacts_secret_and_exception_details(secret_dir, monkeypatch, capsys):
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO('{"password":"private-secret","confirmation":"other-private"}'),
    )
    assert (
        main(
            [
                "initialize",
                "--username",
                "admin",
                "--secret-dir",
                str(secret_dir),
                "--password-stdin",
            ]
        )
        == 2
    )
    output = capsys.readouterr()
    assert "private" not in output.err + output.out
    assert not (secret_dir / "auth.json").exists()
