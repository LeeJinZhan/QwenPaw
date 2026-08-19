from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI

from qwenpaw.plugins.architecture import PluginManifest
from qwenpaw.plugins.loader import PluginLoader
from qwenpaw.plugins.registry import PluginRegistry

PLUGIN_ROOT = Path(__file__).resolve().parents[2]


def test_manifest_is_local_dev_read_only_pawapp() -> None:
    raw = json.loads((PLUGIN_ROOT / "plugin.json").read_text(encoding="utf-8"))
    manifest = PluginManifest.from_dict(raw)

    assert manifest.id == "bank-runtime-console"
    assert manifest.plugin_type.value == "app"
    assert raw["meta"]["permissions"] == {"chat": False, "storage": False}
    assert raw["meta"]["deployment"] == {
        "allowed_profiles": ["local", "dev"],
        "production_default": "not-installed",
    }


@pytest.mark.asyncio
async def test_pawapp_loads_through_official_plugin_loader() -> None:
    previous = PluginRegistry._instance
    PluginRegistry._instance = None
    registry = PluginRegistry()
    registry.set_plugin_http_app(FastAPI())
    try:
        loader = PluginLoader(plugin_dirs=[PLUGIN_ROOT.parent])
        discovered = {
            manifest.id: (manifest, source)
            for manifest, source in loader.discover_plugins()
        }
        manifest, source = discovered["bank-runtime-console"]
        record = await loader.load_plugin(manifest, source)

        assert record.enabled is True
        assert [item.prefix for item in registry.get_http_router_registrations()] == [
            "/bank-runtime-console"
        ]
    finally:
        PluginRegistry._instance = previous
