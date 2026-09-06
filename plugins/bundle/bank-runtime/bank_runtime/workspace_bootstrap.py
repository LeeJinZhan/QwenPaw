"""Explicit first-install configuration; never copy a developer workspace."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
from pathlib import Path
import sys

from qwenpaw.config.config import AgentProfileConfig, Config

from .admin_bootstrap import _lock, _private_path, _publish, verify_administrator

_RECEIPT = ".bank-runtime-bootstrap.json"
_SCHEMA = "bank-runtime-workspace/v1"
_AGENT = "workspaces/bank-assistant/agent.json"


def _json(value):
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def _templates(trusted_proxies):
    if not isinstance(trusted_proxies, list) or not trusted_proxies:
        raise ValueError("trusted_proxies_required")
    proxies = []
    for value in trusted_proxies:
        network = ipaddress.ip_network(value, strict=False)
        if network.prefixlen == 0:
            raise ValueError("unrestricted_proxy_not_allowed")
        proxies.append(str(network))
    root = Path(__file__).resolve().parents[1]
    config = json.loads((root / "production-config.example.json").read_text())
    agent = json.loads((root / "production-agent.example.json").read_text())
    config["security"]["trusted_proxies"] = proxies
    agent["security"]["trusted_proxies"] = proxies
    Config.model_validate(config)
    AgentProfileConfig.model_validate(agent)
    return {"config.json": _json(config), _AGENT: _json(agent)}


def _validate_existing(working):
    paths = [working / "config.json", working / _AGENT]
    for path in paths:
        _private_path(path, required=True)
    config = Config.model_validate_json(paths[0].read_text())
    agent = AgentProfileConfig.model_validate_json(paths[1].read_text())
    if agent.id != "bank-assistant" or "bank-assistant" not in config.agents.profiles:
        raise ValueError("worker_agent_binding_mismatch")


def _mineru_token(secret_dir, supplied):
    path = secret_dir / "mineru.token"
    _private_path(path)
    if path.exists():
        value = path.read_text().removesuffix("\n")
        if supplied and supplied != value:
            raise ValueError("existing_mineru_token_conflict")
    else:
        value = supplied
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 8192
        or any(c in value for c in "\r\n\0")
    ):
        raise ValueError("invalid_mineru_token")
    if not path.exists():
        _publish(path, value + "\n")


def initialize_workspace(
    working: Path, secret_dir: Path, *, trusted_proxies, mineru_token=""
):
    verify_administrator(secret_dir)
    files = _templates(trusted_proxies)
    with _lock(working):
        receipt = working / _RECEIPT
        _private_path(receipt)
        if receipt.exists():
            record = json.loads(receipt.read_text())
            if record.get("schema") != _SCHEMA:
                raise ValueError("unknown_bootstrap_receipt")
            if record.get("state") == "complete":
                _validate_existing(working)
                _mineru_token(secret_dir, mineru_token)
                return "already_exists"
        else:
            if any(path.name != ".admin-bootstrap.lock" for path in working.iterdir()):
                raise ValueError("existing_workspace_requires_review")
            record = {
                "schema": _SCHEMA,
                "state": "preparing",
                "files": {
                    name: hashlib.sha256(value.encode()).hexdigest()
                    for name, value in files.items()
                },
            }
            _publish(receipt, _json(record))
        if record.get("state") != "preparing" or record.get("files") != {
            name: hashlib.sha256(value.encode()).hexdigest()
            for name, value in files.items()
        }:
            raise ValueError("incomplete_bootstrap_requires_original_inputs")
        # Resume only this install's exact intended files. Never overwrite an
        # existing administrator-modified or unrelated configuration.
        for name, value in files.items():
            path = working / name
            _private_path(path)
            if path.exists() and path.read_text() != value:
                raise ValueError("partial_workspace_content_conflict")
        _mineru_token(secret_dir, mineru_token)
        for name, value in files.items():
            path = working / name
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not path.exists():
                _publish(path, value)
        _validate_existing(working)
        record["state"] = "complete"
        _publish(receipt, _json(record), replace=True)
        return "created"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--working-dir", type=Path, default=Path("/app/working"))
    parser.add_argument("--secret-dir", type=Path, default=Path("/app/working.secret"))
    parser.add_argument("--input-stdin", action="store_true", required=True)
    args = parser.parse_args(argv)
    try:
        data = json.loads(sys.stdin.read(65537))
        result = initialize_workspace(
            args.working_dir,
            args.secret_dir,
            trusted_proxies=data["trusted_proxies"],
            mineru_token=data.get("mineru_token", ""),
        )
        print("QWENPAW_CONFIG_READY " + result)
        return 0
    except (KeyboardInterrupt, EOFError):
        print("QwenPaw 初始配置已中断，使用原输入重试。", file=sys.stderr)
        return 130
    except Exception:
        print(
            "QwenPaw 初始配置未完成，请核对原生管理员、MinerU 凭据、目录权限及原初始化记录；不覆盖现有配置。",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
