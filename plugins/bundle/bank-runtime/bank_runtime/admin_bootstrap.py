"""Offline provisioning of QwenPaw's native single administrator, before exposure."""

from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
import fcntl
import getpass
import hashlib
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
import warnings

from cryptography.fernet import Fernet


def _private_path(path: Path, *, required=False):
    if not path.is_absolute() or any(
        item.is_symlink() for item in (path, *path.parents)
    ):
        raise ValueError("unsafe_auth_path")
    if path.exists() and (
        not path.is_file()
        or path.stat().st_mode & 0o077
        or path.stat().st_uid != os.geteuid()
    ):
        raise ValueError("unsafe_auth_file")
    if required and not path.is_file():
        raise ValueError("auth_file_missing")


def _key(directory: Path, *, required=False):
    path = directory / ".master_key"
    _private_path(path, required=required)
    if not path.exists():
        return None
    key = bytes.fromhex(path.read_text().strip())
    if len(key) != 32:
        raise ValueError("invalid_auth_key")
    return key


def _read(directory: Path):
    path = directory / "auth.json"
    _private_path(path, required=True)
    key = _key(directory, required=True)
    data = json.loads(path.read_text())
    user = data.get("user", {})
    if (
        not isinstance(user, dict)
        or not isinstance(user.get("username"), str)
        or not user["username"].strip()
    ):
        raise ValueError("native_administrator_missing")
    if (
        len(bytes.fromhex(user.get("password_hash", ""))) != 32
        or len(bytes.fromhex(user.get("password_salt", ""))) != 16
    ):
        raise ValueError("invalid_native_password_hash")
    encrypted = data.get("jwt_secret", "")
    if not isinstance(encrypted, str) or not encrypted.startswith("ENC:"):
        raise ValueError("native_jwt_secret_not_encrypted")
    # Native decrypt intentionally tolerates bad keys; deployment must fail closed.
    jwt = Fernet(base64.urlsafe_b64encode(key)).decrypt(encrypted[4:].encode()).decode()
    if len(jwt) < 32:
        raise ValueError("invalid_native_jwt_secret")
    return data, key


def _publish(path: Path, value: str, *, replace=False):
    descriptor, name = tempfile.mkstemp(prefix=".admin-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(name, path)
        else:
            os.link(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


@contextmanager
def _lock(directory: Path):
    if not directory.is_absolute() or any(
        item.is_symlink() for item in (directory, *directory.parents)
    ):
        raise ValueError("unsafe_secret_directory")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if directory.stat().st_mode & 0o077 or directory.stat().st_uid != os.geteuid():
        raise ValueError("unsafe_secret_directory")
    descriptor = os.open(
        directory / ".admin-bootstrap.lock",
        os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
        0o600,
    )
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(descriptor)


def verify_administrator(directory: Path):
    data, _ = _read(directory)
    return data["user"]["username"]


def provision_administrator(
    directory: Path, username: str, password: str, confirmation: str, *, reset=False
):
    username = username.strip()
    if not username or len(username) > 80 or any(char in username for char in "\r\n\0"):
        raise ValueError("invalid_administrator_name")
    if (
        not isinstance(password, str)
        or password != confirmation
        or not 12 <= len(password) <= 4096
    ):
        raise ValueError("password_confirmation_failed")
    with _lock(directory):
        auth = directory / "auth.json"
        _private_path(auth)
        exists = auth.exists()
        if exists:
            previous, key = _read(directory)
            if previous["user"]["username"] != username:
                raise ValueError("administrator_name_conflict")
            if not reset:
                return "already_exists"
        else:
            if reset:
                raise ValueError("administrator_missing_for_reset")
            key = _key(directory)
            if key is None:
                if any(
                    item.name != ".admin-bootstrap.lock" for item in directory.iterdir()
                ):
                    raise ValueError("existing_secrets_require_original_key")
                key = secrets.token_bytes(32)
                _publish(directory / ".master_key", key.hex() + "\n")
        salt = secrets.token_hex(16)
        hashed = hashlib.sha256((salt + password).encode()).hexdigest()
        encrypted = (
            "ENC:"
            + Fernet(base64.urlsafe_b64encode(key))
            .encrypt(secrets.token_hex(32).encode())
            .decode()
        )
        data = {
            "user": {
                "username": username,
                "password_hash": hashed,
                "password_salt": salt,
            },
            "jwt_secret": encrypted,
            "revoked_tokens": [],
            "revoked_tokens_meta": {},
        }
        _publish(
            auth, json.dumps(data, ensure_ascii=False, indent=2) + "\n", replace=exists
        )
        _read(directory)
        return "reset" if reset else "created"


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Offline native QwenPaw administrator; never output login tokens."
    )
    parser.add_argument("action", choices=("initialize", "verify", "reset"))
    parser.add_argument(
        "--secret-dir",
        type=Path,
        default=Path(os.environ.get("QWENPAW_SECRET_DIR", "/app/working.secret")),
    )
    parser.add_argument("--username", default="")
    parser.add_argument("--password-stdin", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.action == "verify":
            verify_administrator(args.secret_dir)
            result = "verified"
        else:
            if args.password_stdin:
                data = json.loads(sys.stdin.read(65537))
                password, confirmation = data["password"], data["confirmation"]
            else:
                if not sys.stdin.isatty():
                    raise ValueError("interactive_terminal_required")
                with warnings.catch_warnings():
                    warnings.simplefilter("error", getpass.GetPassWarning)
                    password = getpass.getpass("QwenPaw 管理员密码：")
                    confirmation = getpass.getpass("再次输入管理员密码：")
            result = provision_administrator(
                args.secret_dir,
                args.username,
                password,
                confirmation,
                reset=args.action == "reset",
            )
        print("QWENPAW_ADMIN_READY " + result)
        return 0
    except (KeyboardInterrupt, EOFError):
        print("QwenPaw 管理员操作已取消。", file=sys.stderr)
        return 130
    except Exception:
        print(
            "QwenPaw 管理员校验或配置未完成，请核对输入、原密钥及文件权限；不自动重置已有账号。",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
