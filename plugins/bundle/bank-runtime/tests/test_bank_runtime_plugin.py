from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from qwenpaw.plugins.api import PluginApi
from qwenpaw.plugins.architecture import PluginManifest
from qwenpaw.plugins.loader import PluginLoader
from qwenpaw.plugins.registry import PluginRegistry

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = PLUGIN_ROOT / "plugin.json"
ENTRY_PATH = PLUGIN_ROOT / "plugin.py"
DELIVERY_PATH = PLUGIN_ROOT / "delivery-manifest.json"


@pytest.fixture
def fresh_registry():
    previous = PluginRegistry._instance
    PluginRegistry._instance = None
    registry = PluginRegistry()
    try:
        yield registry
    finally:
        PluginRegistry._instance = previous


def _load_entry_module():
    root = str(PLUGIN_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    spec = importlib.util.spec_from_file_location(
        "bank_runtime_plugin_entry_for_test",
        ENTRY_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _plugin_api(registry: PluginRegistry, manifest: dict) -> PluginApi:
    api = PluginApi("bank-runtime", config={}, manifest=manifest)
    api.set_registry(registry)
    return api


def test_manifest_is_strictly_scoped_to_qwenpaw_2_1() -> None:
    manifest_data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    manifest = PluginManifest.from_dict(manifest_data)

    assert manifest.id == "bank-runtime"
    assert manifest.entry.backend == "plugin.py"
    assert manifest_data["qwenpaw_version"] == {
        "min": "2.1.0",
        "max": "2.2.0",
    }


def test_delivery_manifest_pins_source_and_blocks_unknown_image_digest() -> None:
    delivery = json.loads(DELIVERY_PATH.read_text(encoding="utf-8"))

    assert delivery["schema_version"] == "bank-runtime-delivery/v1"
    assert delivery["qwenpaw_upstream_commit"] == (
        "e4995dcf516d27400fbc33891aa3dcbcf79acc7a"
    )
    assert delivery["bank_runtime_plugin_version"] == "0.6.0"
    assert delivery["runtime_release_id"] == "candidate-2.1-console"
    assert delivery["optional_pawapps"] == {
        "local": ["bank-runtime-console@0.1.0"],
        "dev": ["bank-runtime-console@0.1.0"],
        "production": [],
    }
    assert delivery["bank_runtime_protocol_versions"][-2:] == [
        "sandbox-files/2.0",
        "physical-sandbox/1.0",
    ]
    assert delivery["stable_rollback"] == {
        "git_ref": "refs/heads/rollback/bank-runtime-1.1.12-92785ad6",
        "git_commit": "92785ad6a64ec0e11e2a59ba8aeac5bee60cb450",
        "image_digest": None,
    }
    assert delivery["candidate_image_digest"] is None
    assert delivery["promotion_blockers"] == [
        "stable_image_digest_missing",
        "candidate_image_digest_missing",
    ]


def test_plugin_registers_router_channel_hook_and_middleware(
    fresh_registry: PluginRegistry,
) -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    app = FastAPI()
    fresh_registry.set_plugin_http_app(app)
    module = _load_entry_module()

    module.BankRuntimePlugin().register(_plugin_api(fresh_registry, manifest))

    assert [item.prefix for item in fresh_registry.get_http_router_registrations()] == [
        "/bank-runtime",
    ]
    assert set(fresh_registry.get_registered_channels()) == {"bank-runtime"}
    assert [item.hook_name for item in fresh_registry.get_startup_hooks()] == [
        "bank_runtime_startup_guard",
        "register_tool_bank-runtime_bank_assistant",
        "register_tool_bank-runtime_activate_personal_skill",
        "rt_hook_bank-runtime_bank_runtime_session_prepare",
        "rt_hook_bank-runtime_bank_runtime_disable_long_term_memory",
        "rt_hook_bank-runtime_bank_runtime_personalization",
        "rt_hook_bank-runtime_bank_runtime_gateway_install",
        "rt_hook_bank-runtime_bank_runtime_sandbox_install",
        "rt_hook_bank-runtime_bank_runtime_attachment_prepare",
        "rt_hook_bank-runtime_bank_runtime_personalization_redaction",
        "rt_hook_bank-runtime_bank_runtime_session_commit",
        "rt_hook_bank-runtime_bank_runtime_session_error",
        "rt_hook_bank-runtime_bank_runtime_personalization_cleanup",
        "rt_hook_bank-runtime_bank_runtime_sandbox_cleanup",
        "rt_hook_bank-runtime_bank_runtime_session_cleanup",
        "install_skills_bank-runtime",
    ]
    middleware = fresh_registry.get_middleware_factories()
    assert len(middleware) == 1
    assert middleware[0].plugin_id == "bank-runtime"


@pytest.mark.asyncio
async def test_plugin_is_discovered_and_loaded_by_qwenpaw_loader(
    fresh_registry: PluginRegistry,
) -> None:
    app = FastAPI()
    fresh_registry.set_plugin_http_app(app)
    loader = PluginLoader(plugin_dirs=[PLUGIN_ROOT.parent])
    discovered = {
        manifest.id: (manifest, source_path)
        for manifest, source_path in loader.discover_plugins()
    }

    manifest, source_path = discovered["bank-runtime"]
    record = await loader.load_plugin(manifest, source_path)

    assert record.enabled is True
    assert record.manifest.id == "bank-runtime"
    assert fresh_registry.get_plugin_manifest("bank-runtime") is not None


def test_capability_endpoint_is_safe_and_declares_managed_session_state(
    fresh_registry: PluginRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QWENPAW_SERVICE_TOKEN", "candidate-service-secret")
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    app = FastAPI()
    fresh_registry.set_plugin_http_app(app)
    module = _load_entry_module()
    module.BankRuntimePlugin().register(_plugin_api(fresh_registry, manifest))

    response = TestClient(app).get(
        "/api/bank-runtime/capabilities",
        headers={
            "Authorization": "Bearer candidate-service-secret",
            "X-Agent-Id": "bank-assistant",
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "qwenpaw_version": "2.1.0",
        "bank_runtime_plugin_version": "0.6.0",
        "protocols": [
            "bank-runtime-text-sse/v1",
            "session/2.0",
            "profile/1.0",
            "personal-skills/1.0",
            "bank-assistant/1.0",
            "tool-gateway/2.0",
            "sandbox-files/2.0",
            "physical-sandbox/1.0",
        ],
        "capabilities": {
            "agent_scoped_chat": True,
            "managed_session": True,
            "gateway_middleware": True,
            "attachment_batch_authorize": True,
            "sandbox_file_search_select": True,
            "personal_skills": True,
            "runtime_console_readonly": False,
        },
        "disabled_features": [
            "os_shell",
            "creator",
            "browser_use",
            "computer_use",
            "external_harnesses",
            "market_plugins",
            "long_term_memory",
        ],
    }
    serialized = response.text.lower()
    for forbidden in ("token", "credential", "path", "model", "user"):
        assert forbidden not in serialized


@pytest.mark.parametrize(
    ("headers", "expected_status"),
    [
        ({"X-Agent-Id": "bank-assistant"}, 401),
        (
            {
                "Authorization": "Bearer wrong-secret",
                "X-Agent-Id": "bank-assistant",
            },
            401,
        ),
        ({"Authorization": "Bearer candidate-service-secret"}, 401),
    ],
)
def test_capability_endpoint_fails_closed_without_trusted_service_identity(
    fresh_registry: PluginRegistry,
    monkeypatch: pytest.MonkeyPatch,
    headers: dict[str, str],
    expected_status: int,
) -> None:
    monkeypatch.setenv("QWENPAW_SERVICE_TOKEN", "candidate-service-secret")
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    app = FastAPI()
    fresh_registry.set_plugin_http_app(app)
    module = _load_entry_module()
    module.BankRuntimePlugin().register(_plugin_api(fresh_registry, manifest))

    response = TestClient(app).get(
        "/api/bank-runtime/capabilities",
        headers=headers,
    )

    assert response.status_code == expected_status
    assert "candidate-service-secret" not in response.text
    assert "wrong-secret" not in response.text


def test_capability_endpoint_fails_closed_when_service_token_is_not_configured(
    fresh_registry: PluginRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("QWENPAW_SERVICE_TOKEN", raising=False)
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    app = FastAPI()
    fresh_registry.set_plugin_http_app(app)
    module = _load_entry_module()
    module.BankRuntimePlugin().register(_plugin_api(fresh_registry, manifest))

    response = TestClient(app).get(
        "/api/bank-runtime/capabilities",
        headers={"Authorization": "Bearer any", "X-Agent-Id": "bank-assistant"},
    )

    assert response.status_code == 503


def test_duplicate_registration_fails_before_adding_partial_state(
    fresh_registry: PluginRegistry,
) -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    app = FastAPI()
    fresh_registry.set_plugin_http_app(app)
    module = _load_entry_module()
    plugin = module.BankRuntimePlugin()
    api = _plugin_api(fresh_registry, manifest)
    plugin.register(api)
    expected_routes = list(app.routes)

    with pytest.raises(RuntimeError, match="already registered"):
        plugin.register(api)

    assert app.routes == expected_routes
    assert len(fresh_registry.get_http_router_registrations()) == 1
    assert len(fresh_registry.get_startup_hooks()) == 16
    assert len(fresh_registry.get_middleware_factories()) == 1

    with pytest.raises(ValueError, match="already registered"):
        module.BankRuntimePlugin().register(api)

    assert len(fresh_registry.get_http_router_registrations()) == 1
    assert len(fresh_registry.get_startup_hooks()) == 16
    assert len(fresh_registry.get_middleware_factories()) == 1
