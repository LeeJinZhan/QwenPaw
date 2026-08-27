from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


class _Api:
    def __init__(self) -> None:
        self.startup = []
        self.shutdown = []
        self.uninstall = []

    def register_startup_hook(self, **value):
        self.startup.append(value)

    def register_shutdown_hook(self, **value):
        self.shutdown.append(value)

    def register_uninstall_hook(self, **value):
        self.uninstall.append(value)


def _plugin_module():
    spec = importlib.util.spec_from_file_location(
        "test_bank_mineru_plugin",
        PLUGIN_ROOT / "plugin.py",
        submodule_search_locations=[str(PLUGIN_ROOT)],
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    module.__package__ = spec.name
    module.__path__ = [str(PLUGIN_ROOT)]
    spec.loader.exec_module(module)
    return module


def test_manifest_declares_plugin_dependency_as_metadata_only() -> None:
    manifest = json.loads((PLUGIN_ROOT / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["id"] == "bank-mineru-mcp"
    assert manifest["dependencies"] == []
    assert manifest["meta"]["requires_plugins"] == ["bank-runtime"]


def test_plugin_registers_managed_start_stop_and_uninstall_hooks() -> None:
    module = _plugin_module()
    api = _Api()
    instance = module.BankMinerUMcpPlugin()
    instance.register(api)

    assert [item["hook_name"] for item in api.startup] == ["bank_mineru_mcp_start"]
    assert [item["hook_name"] for item in api.shutdown] == ["bank_mineru_mcp_stop"]
    assert [item["hook_name"] for item in api.uninstall] == [
        "bank_mineru_mcp_uninstall"
    ]
