import json
from pathlib import Path
import sys

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from bank_runtime.admin_bootstrap import provision_administrator
from bank_runtime import workspace_bootstrap as bootstrap
from bank_runtime.production_guard import (
    validate_production_root_config,
    validate_production_agent_profile,
)


@pytest.fixture
def installation(tmp_path):
    secret = tmp_path / "secret"
    provision_administrator(secret, "admin", "long-password-123", "long-password-123")
    return tmp_path / "working", secret


def initialize(installation):
    work, secret = installation
    return bootstrap.initialize_workspace(
        work,
        secret,
        trusted_proxies=["10.20.0.0/16"],
        mineru_token="mineru-test-secret",
    )


def test_initial_config_aligns_general_assistant_and_governed_document_tools(
    installation,
):
    assert initialize(installation) == "created"
    work, secret = installation
    root = json.loads((work / "config.json").read_text())
    agent = json.loads((work / bootstrap._AGENT).read_text())
    assert root["agents"]["active_agent"] == agent["id"] == "bank-assistant"
    assert agent["name"] == "通用助手"
    assert root["plugins"]["bank-mineru-mcp"]["enabled"] is True
    for config in [root, agent]:
        tools = config["tools"]["builtin_tools"]
        assert {name for name, item in tools.items() if item["enabled"]} == {
            "activate_personal_skill",
            "artifact_generate",
            "artifact_revise",
            "artifact_convert",
        }
        assert config["mcp"]["clients"]["mineru"]["name"] == "MinerU"
        assert config["mcp"]["clients"]["mineru"]["tools"] == [
            "parse_documents",
            "read_document_chunks",
        ]
        assert config["security"]["trusted_proxies"] == ["10.20.0.0/16"]
        assert config["security"]["allow_no_auth_hosts"] == []
    from types import SimpleNamespace
    from qwenpaw.drivers.handlers.mcp import _mcp_tool_to_capability

    client = agent["mcp"]["clients"]["mineru"]
    for raw_name in client["tools"]:
        capability = _mcp_tool_to_capability(
            "mineru", SimpleNamespace(name=raw_name), display_name=client["name"]
        )
        assert capability.exposure.tool_name == "MinerU__" + raw_name
    assert validate_production_root_config(root).ready
    assert validate_production_agent_profile(agent).ready
    assert (secret / "mineru.token").read_text() == "mineru-test-secret\n"
    assert (secret / "mineru.token").stat().st_mode & 0o777 == 0o600
    assert "mineru-test-secret" not in (work / "config.json").read_text()


def test_repeat_preserves_admin_model_configuration_and_native_auth(installation):
    initialize(installation)
    work, secret = installation
    path = work / "config.json"
    config = json.loads(path.read_text())
    config["language"] = "zh"
    path.write_text(json.dumps(config))
    provider = secret / "providers/custom/intranet-deepseek.json"
    provider.parent.mkdir(parents=True, mode=0o700)
    provider.write_text(
        json.dumps(
            {
                "id": "intranet-deepseek",
                "base_url": "http://model.internal/v1",
                "api_key": "ENC:fixture",
            }
        )
    )
    before = {
        p: p.read_bytes()
        for p in [
            path,
            work / bootstrap._AGENT,
            secret / "auth.json",
            secret / ".master_key",
            provider,
        ]
    }
    assert initialize(installation) == "already_exists"
    assert all(p.read_bytes() == value for p, value in before.items())


def test_interrupted_initialization_resumes_only_matching_intended_files(
    installation, monkeypatch
):
    original = bootstrap._publish

    def fail(path, value, **options):
        if path.name == "agent.json":
            raise OSError("interrupted")
        return original(path, value, **options)

    monkeypatch.setattr(bootstrap, "_publish", fail)
    with pytest.raises(OSError):
        initialize(installation)
    work, _ = installation
    assert (work / "config.json").is_file()
    assert json.loads((work / bootstrap._RECEIPT).read_text())["state"] == "preparing"
    monkeypatch.setattr(bootstrap, "_publish", original)
    assert initialize(installation) == "created"


@pytest.mark.parametrize("existing", ["foreign", "partial-conflict", "token-conflict"])
def test_existing_state_is_never_silently_overwritten(installation, existing):
    work, secret = installation
    if existing == "foreign":
        work.mkdir()
        (work / "config.json").write_text("unrelated")
    else:
        initialize(installation)
        if existing == "partial-conflict":
            receipt = work / bootstrap._RECEIPT
            data = json.loads(receipt.read_text())
            data["state"] = "preparing"
            receipt.write_text(json.dumps(data))
            (work / "config.json").write_text("changed")
        else:
            (secret / "mineru.token").write_text("other-secret")
    before = {
        p: p.read_bytes()
        for directory in [work, secret]
        for p in directory.rglob("*")
        if p.is_file()
    }
    with pytest.raises(ValueError):
        initialize(installation)
    assert all(p.read_bytes() == value for p, value in before.items())


def test_missing_native_admin_and_unrestricted_proxy_rejected(installation):
    work, secret = installation
    with pytest.raises(ValueError):
        bootstrap.initialize_workspace(
            work, secret, trusted_proxies=["0.0.0.0/0"], mineru_token="test"
        )
    assert not work.exists()
    (secret / "auth.json").unlink()
    with pytest.raises(ValueError):
        initialize(installation)
    assert not work.exists()
