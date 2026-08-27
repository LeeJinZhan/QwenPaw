from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from bank_runtime.production_guard import (
    GuardResult,
    ProductionSnapshot,
    apply_production_agent_allowlist,
    apply_production_route_allowlist,
    dependency_probe,
    evaluate_snapshot,
    execute_production_guard,
    get_production_readiness,
    load_production_policy,
    publish_production_readiness,
    reset_production_readiness_for_tests,
    validate_production_agent_profile,
    validate_production_root_config,
)
from bank_runtime.router import build_ingress_router
from bank_runtime.delivery_probe import probe_delivery_files
from qwenpaw.config.config import AgentProfileConfig, Config

POLICY_PATH = PLUGIN_ROOT / "production-policy.json"
PROFILE_PATH = PLUGIN_ROOT / "production-agent.example.json"
ROOT_CONFIG_PATH = PLUGIN_ROOT / "production-config.example.json"
DOCKERFILE_PATH = PLUGIN_ROOT / "Dockerfile.production"
COMPOSE_PATH = PLUGIN_ROOT / "docker-compose.production.yml"
ENTRYPOINT_PATH = PLUGIN_ROOT / "production-entrypoint.sh"
PRODUCTION_LOCK_PATH = PLUGIN_ROOT / "production-python311-linux-amd64.lock"
DOCKERIGNORE_PATH = PLUGIN_ROOT.parents[2] / ".dockerignore"


def _approved_snapshot(**overrides: object) -> ProductionSnapshot:
    data: dict[str, object] = {
        "plugins": {"bank-runtime"},
        "loaded_agents": {"bank-assistant"},
        "plugin_channels": {"bank-runtime"},
        "registered_modes": {"default", "coding", "goal", "mission"},
        "registered_tools": {
            "append_file",
            "ast_search",
            "bank_assistant",
            "browser",
            "chat_with_agent",
            "check_agent_task",
            "create_goal",
            "delegate_external_agent",
            "desktop_screenshot",
            "edit_file",
            "execute_shell_command",
            "get_current_time",
            "get_goal",
            "get_token_usage",
            "glob_search",
            "grep_search",
            "list_agents",
            "materialize_skill",
            "read_file",
            "run_tool_batch",
            "send_file_to_user",
            "set_user_timezone",
            "spawn_subagent",
            "submit_to_agent",
            "update_goal",
            "view_image",
            "view_video",
            "web_fetch",
            "web_search",
            "write_file",
            "activate_personal_skill",
        },
        "reachable_tools": {"bank_assistant", "activate_personal_skill"},
        "enabled_channels": {"bank-runtime"},
        "enabled_mcp_clients": set(),
        "enabled_harnesses": set(),
        "active_features": set(),
        "route_paths": {
            "/api/bank-runtime/capabilities",
            "/api/bank-runtime/agents/{agent_id}/health",
            "/api/bank-runtime/agents/{agent_id}/chat",
            "/api/bank-runtime/agents/{agent_id}/chat/stop",
        },
    }
    data.update(overrides)
    return ProductionSnapshot(**data)


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("reachable_tools", {"execute_shell_command"}, "forbidden_tool"),
        ("reachable_tools", {"browser"}, "forbidden_tool"),
        ("active_features", {"creator"}, "forbidden_feature"),
        ("active_features", {"computer_use"}, "forbidden_feature"),
        ("enabled_harnesses", {"codex"}, "forbidden_harness"),
        ("enabled_harnesses", {"qoder"}, "forbidden_harness"),
        ("active_features", {"long_term_memory"}, "forbidden_feature"),
    ],
)
def test_any_forbidden_capability_makes_readiness_fail(
    field: str,
    value: set[str],
    reason: str,
) -> None:
    policy = load_production_policy(POLICY_PATH)
    snapshot = _approved_snapshot(**{field: value})

    result = evaluate_snapshot(snapshot, policy)

    assert result.ready is False
    assert reason in result.reason_codes
    assert "execute_shell_command" not in result.public_payload()
    assert "market-weather" not in result.public_payload()


@pytest.mark.parametrize(
    ("field", "required", "reason"),
    [
        ("plugins", "bank-runtime", "missing_required_plugin"),
        ("registered_tools", "bank_assistant", "missing_registered_tool"),
        ("registered_modes", "default", "missing_registered_mode"),
        ("plugin_channels", "bank-runtime", "missing_required_channel"),
    ],
)
def test_missing_required_registry_item_blocks_startup(
    field: str,
    required: str,
    reason: str,
) -> None:
    policy = load_production_policy(POLICY_PATH)
    current = getattr(_approved_snapshot(), field)

    result = evaluate_snapshot(
        _approved_snapshot(**{field: set(current) - {required}}),
        policy,
    )

    assert result.ready is False
    assert reason in result.reason_codes


def test_unapproved_agent_still_blocks_startup() -> None:
    result = evaluate_snapshot(
        _approved_snapshot(loaded_agents={"bank-assistant", "unexpected-agent"}),
        load_production_policy(POLICY_PATH),
    )

    assert result.ready is False
    assert "unknown_agent" in result.reason_codes


def test_approved_registry_snapshot_is_ready() -> None:
    result = evaluate_snapshot(
        _approved_snapshot(),
        load_production_policy(POLICY_PATH),
    )

    assert result.ready is True
    assert result.reason_codes == ()


def test_runtime_governed_mcp_and_extra_plugin_registry_do_not_block_startup() -> None:
    baseline = _approved_snapshot()
    result = evaluate_snapshot(
        _approved_snapshot(
            plugins={"bank-runtime", "bank-mineru-mcp"},
            plugin_channels=set(baseline.plugin_channels) | {"plugin-admin-only"},
            registered_modes=set(baseline.registered_modes) | {"document-mode"},
            registered_tools=set(baseline.registered_tools)
            | {"MinerU__parse_documents"},
            enabled_mcp_clients={"mineru"},
        ),
        load_production_policy(POLICY_PATH),
    )

    assert result.ready is True
    assert result.reason_codes == ()


def test_production_route_allowlist_removes_non_runtime_attack_routes() -> None:
    app = FastAPI()

    @app.get("/api/bank-runtime")
    async def bank_mount_root() -> dict[str, str]:
        return {"status": "mounted"}

    @app.get("/api/bank-runtime/agents/{agent_id}/health")
    async def bank_health(agent_id: str) -> dict[str, str]:
        return {"agent_id": agent_id}

    @app.post("/api/creator/generate")
    async def creator() -> dict[str, str]:
        return {"status": "reachable"}

    @app.post("/api/browser/execute")
    async def browser() -> dict[str, str]:
        return {"status": "reachable"}

    removed = apply_production_route_allowlist(
        app,
        load_production_policy(POLICY_PATH),
    )
    client = TestClient(app)

    # FastAPI's docs/OpenAPI routes are removed together with the two
    # intentionally hostile routes.
    assert removed >= 2
    assert client.get("/api/bank-runtime").status_code == 200
    assert client.get("/api/bank-runtime/agents/assistant-a/health").status_code == 200
    assert client.post("/api/creator/generate").status_code == 404
    assert client.post("/api/browser/execute").status_code == 404


def test_production_route_allowlist_keeps_reviewed_plugin_route_identity() -> None:
    app = FastAPI()

    @app.get("/plugin-local-health", name="reviewed_plugin_health")
    async def plugin_local_health() -> dict[str, str]:
        return {"status": "ok"}

    reviewed_route = next(
        route
        for route in app.router.routes
        if getattr(route, "name", "") == "reviewed_plugin_health"
    )

    apply_production_route_allowlist(
        app,
        load_production_policy(POLICY_PATH),
        explicit_allowed_routes=[reviewed_route],
    )

    assert TestClient(app).get("/plugin-local-health").status_code == 200


def test_production_agent_allowlist_blocks_late_default_start() -> None:
    class Manager:
        def __init__(self) -> None:
            self.agents = {"bank-assistant": object()}

        async def get_agent(self, agent_id: str) -> str:
            return agent_id

    manager = Manager()
    apply_production_agent_allowlist(manager, {"bank-assistant"})

    import asyncio

    assert asyncio.run(manager.get_agent("bank-assistant")) == "bank-assistant"
    with pytest.raises(RuntimeError, match="production_agent_not_allowed"):
        asyncio.run(manager.get_agent("default"))


def test_production_agent_profile_disables_external_authority() -> None:
    profile = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))

    result = validate_production_agent_profile(profile)

    assert result.ready is True
    serialized = json.dumps(profile, sort_keys=True).lower()
    for secret_key in (
        "api_key",
        "access_key",
        "secret_key",
        "browser_cdp_url",
    ):
        assert secret_key not in serialized


def test_profile_accepts_enabled_mcp_without_echoing_connection_values() -> None:
    profile = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    profile["mcp"]["clients"]["evil"] = {
        "enabled": True,
        "url": "https://user:very-secret@example.invalid/mcp",
    }

    result = validate_production_agent_profile(profile)

    assert result.ready is True
    assert "enabled_mcp_client" not in result.reason_codes
    assert "very-secret" not in result.public_payload()
    assert "example.invalid" not in result.public_payload()


def test_production_root_config_disables_browser_plugins_and_global_authority() -> None:
    config = json.loads(ROOT_CONFIG_PATH.read_text(encoding="utf-8"))

    result = validate_production_root_config(config)

    assert result.ready is True


def test_root_config_accepts_admin_installed_plugins_and_enabled_mcp() -> None:
    config = json.loads(ROOT_CONFIG_PATH.read_text(encoding="utf-8"))
    config["plugins"]["bank-mineru-mcp"] = {"enabled": True}
    config["mcp"]["clients"]["mineru"] = {
        "enabled": True,
        "url": "http://127.0.0.1:18081/mcp",
    }

    result = validate_production_root_config(config)

    assert result.ready is True


def test_root_browser_authority_is_rejected_without_echoing_endpoint() -> None:
    config = json.loads(ROOT_CONFIG_PATH.read_text(encoding="utf-8"))
    config["browser"]["cdp_url"] = "ws://user:secret@browser.invalid/devtools"

    result = validate_production_root_config(config)

    assert result.ready is False
    assert "browser_authority_present" in result.reason_codes
    assert "browser.invalid" not in result.public_payload()


def test_delivery_examples_survive_native_qwenpaw_validation() -> None:
    root = Config.model_validate(
        json.loads(ROOT_CONFIG_PATH.read_text(encoding="utf-8"))
    )
    agent = AgentProfileConfig.model_validate(
        json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    )

    assert root.browser.experimental is False
    assert root.agents.active_agent == "bank-assistant"
    assert root.agents.agent_order == ["bank-assistant"]
    assert set(root.agents.profiles) == {
        "bank-assistant",
        "default",
        "QwenPaw_QA_Agent_0.2",
    }
    assert root.agents.profiles["bank-assistant"].enabled is True
    assert root.agents.profiles["default"].enabled is False
    assert root.agents.profiles["QwenPaw_QA_Agent_0.2"].enabled is False
    assert root.agents.profiles["bank-assistant"].workspace_dir == agent.workspace_dir
    assert root.channels.console.enabled is False
    assert root.channels.__pydantic_extra__["bank-runtime"]["enabled"] is True
    assert all(not item.enabled for item in root.acp.agents.values())
    assert {
        name for name, item in root.tools.builtin_tools.items() if item.enabled
    } == {"bank_assistant", "activate_personal_skill"}
    assert agent.running.reme_light_memory_config.memory_search_enabled is False
    assert agent.running.reme_light_memory_config.dream_cron_enabled is False


def test_root_config_rejects_enabled_builtin_agent() -> None:
    config = json.loads(ROOT_CONFIG_PATH.read_text(encoding="utf-8"))
    config["agents"]["profiles"]["default"]["enabled"] = True

    result = validate_production_root_config(config)

    assert result.ready is False
    assert result.reason_codes == ("unapproved_agent_profile",)


def test_delivery_file_probe_combines_native_config_and_import_checks() -> None:
    result = probe_delivery_files(
        root_config_path=ROOT_CONFIG_PATH,
        agent_config_paths=[PROFILE_PATH],
        importer=lambda _name: object(),
    )

    assert result.ready is True


def test_delivery_file_probe_redacts_invalid_json(tmp_path: Path) -> None:
    bad = tmp_path / "config-secret.json"
    bad.write_text('{"password":"very-secret",', encoding="utf-8")

    result = probe_delivery_files(
        root_config_path=bad,
        agent_config_paths=[PROFILE_PATH],
        importer=lambda _name: object(),
    )

    assert result.ready is False
    assert result.reason_codes == ("invalid_root_config",)
    assert "very-secret" not in result.public_payload()


def test_production_image_omits_browser_desktop_and_market_plugin_payloads() -> None:
    dockerfile = DOCKERFILE_PATH.read_text(encoding="utf-8").lower()

    for forbidden in (
        "chromium-sandbox",
        "xfce4",
        "desktop_screenshot",
        "plugins/apps",
        "plugins/tool",
        "plugins/channel",
    ):
        assert forbidden not in dockerfile
    assert "plugins/bundle/bank-runtime" in dockerfile
    assert "production-python311-linux-amd64.lock" in dockerfile
    assert "/app/task-files" in dockerfile
    assert "node:" not in dockerfile
    assert "npm " not in dockerfile
    assert "console-builder" not in dockerfile
    assert "qwenpaw_secret_dir" not in dockerfile
    assert "src/qwenpaw/console" in DOCKERIGNORE_PATH.read_text(encoding="utf-8")


def test_production_image_pins_base_and_embeds_source_identity() -> None:
    dockerfile = DOCKERFILE_PATH.read_text(encoding="utf-8")

    assert (
        "ARG PYTHON_IMAGE=python:3.11-slim-bookworm@"
        "sha256:2e32f7d302adc1c37428355c1e646897c0c53f4fd60b6a551245fb90ee129f91"
        in dockerfile
    )
    assert dockerfile.count("ARG PYTHON_IMAGE") == 2
    assert (
        "COPY plugins/bundle/bank-runtime/production-python311-linux-amd64.lock"
        in dockerfile
    )
    assert "--require-hashes" in dockerfile
    assert "--no-deps --no-build-isolation ." in dockerfile
    assert "python -m pip uninstall -y pip wheel setuptools" in dockerfile
    assert 'find_spec("pip") is None' in dockerfile
    assert 'find_spec("setuptools") is None' in dockerfile
    assert 'find_spec("pkg_resources") is None' in dockerfile
    assert (
        "from qwenpaw.app.channels.registry import get_channel_registry" in dockerfile
    )
    assert '"console" in get_channel_registry()' in dockerfile
    assert "ARG BANK_RUNTIME_SOURCE_COMMIT" in dockerfile
    assert "source_commit_invalid" in dockerfile
    assert (
        'org.opencontainers.image.revision="${BANK_RUNTIME_SOURCE_COMMIT}"'
        in dockerfile
    )
    assert (
        'io.bank-runtime.qwenpaw.upstream-revision="e4995dcf516d27400fbc33891aa3dcbcf79acc7a"'
        in dockerfile
    )


def test_production_python_lock_is_exact_and_hash_verified() -> None:
    lock = PRODUCTION_LOCK_PATH.read_text(encoding="utf-8")

    assert "Generated by generate_qwenpaw_pypi_hash_lock.py" in lock
    assert "--hash=sha256:" in lock
    assert "pip==26.2.1" in lock
    assert "setuptools==79.0.1" in lock
    assert "wheel==0.46.3" in lock
    logical_lines: list[str] = []
    pending = ""
    for raw_line in lock.splitlines():
        line = raw_line.strip()
        if (
            not line
            or line.startswith("#")
            or (line.startswith("--") and not line.startswith("--hash="))
        ):
            continue
        pending += line.removesuffix("\\").strip()
        if line.endswith("\\"):
            continue
        logical_lines.append(pending)
        pending = ""
    assert pending == ""
    assert len(logical_lines) >= 200
    for line in logical_lines:
        assert "==" in line
        assert "--hash=sha256:" in line


def test_production_compose_has_no_host_port_or_extra_system_authority() -> None:
    compose = COMPOSE_PATH.read_text(encoding="utf-8")

    assert "BANK_RUNTIME_SOURCE_COMMIT:" in compose
    assert 'BANK_RUNTIME_PRODUCTION_GUARD: "1"' in compose
    assert "QWENPAW_TASK_FILE_ROOT: /app/task-files" in compose
    assert "ports:" not in compose
    assert "cap_drop:" in compose and "- ALL" in compose
    assert "no-new-privileges:true" in compose
    for forbidden in ("docker.sock", "/dev/", "privileged: true"):
        assert forbidden not in compose


def test_production_entrypoint_allows_admin_plugins_and_rejects_insecure_defaults() -> (
    None
):
    entrypoint = ENTRYPOINT_PATH.read_text(encoding="utf-8")

    assert "unexpected_plugin_directory" not in entrypoint
    assert "production_config_missing" in entrypoint
    assert "production_directory_not_writable" in entrypoint
    assert 'task_file_dir="${QWENPAW_TASK_FILE_ROOT:-/app/task-files}"' in entrypoint
    assert '"$task_file_dir"' in entrypoint
    assert "qwenpaw init --defaults" not in entrypoint
    assert 'export PYTHONPATH="/opt/bank-runtime-plugin' in entrypoint
    assert "bank_runtime.delivery_probe" in entrypoint


def test_mineru_plugin_is_packaged_with_loopback_only_deployment_settings() -> None:
    dockerfile = DOCKERFILE_PATH.read_text(encoding="utf-8")
    compose = COMPOSE_PATH.read_text(encoding="utf-8")

    assert "plugins/bundle/bank-mineru-mcp" in dockerfile
    assert "BANK_MINERU_BASE_URL:" in compose
    assert "BANK_MINERU_TOKEN_FILE: /app/working.secret/mineru.token" in compose
    assert 'BANK_MINERU_MCP_HOST: "127.0.0.1"' in compose
    assert 'BANK_MINERU_MCP_PORT: "18081"' in compose
    assert "18081:18081" not in compose


def test_dependency_probe_reports_categories_without_import_details() -> None:
    imported: list[str] = []

    def importer(name: str) -> object:
        imported.append(name)
        if name == "oss2":
            raise ImportError("secret path /srv/credentials/oss")
        return object()

    result = dependency_probe(importer=importer)

    assert result.ready is False
    assert result.reason_codes == ("missing_dependency_oss",)
    assert set(imported) == {
        "oss2",
        "pypdf",
        "httpx",
        "cryptography",
        "zipfile",
        "xml.etree.ElementTree",
    }
    assert "/srv/credentials/oss" not in result.public_payload()


def test_strict_health_fails_closed_with_redacted_readiness(monkeypatch) -> None:
    monkeypatch.setenv("BANK_RUNTIME_PRODUCTION_GUARD", "1")
    monkeypatch.setenv("QWENPAW_SERVICE_TOKEN", "candidate-secret")
    publish_production_readiness(
        GuardResult(
            ready=False,
            reason_codes=("unknown_plugin", "forbidden_tool"),
        )
    )
    app = FastAPI()
    app.include_router(build_ingress_router(), prefix="/api/bank-runtime")
    try:
        response = TestClient(app).get(
            "/api/bank-runtime/agents/bank-assistant/health",
            headers={
                "Authorization": "Bearer candidate-secret",
                "X-Agent-Id": "bank-assistant",
            },
        )
    finally:
        reset_production_readiness_for_tests()

    assert response.status_code == 503
    assert response.json()["detail"] == {
        "code": "bank_runtime_not_ready",
        "reason_codes": ["unknown_plugin", "forbidden_tool"],
    }
    assert "candidate-secret" not in response.text


def test_production_readiness_state_starts_not_evaluated() -> None:
    reset_production_readiness_for_tests()

    result = get_production_readiness()

    assert result.ready is False
    assert result.reason_codes == ("startup_guard_not_run",)


def _strict_registry(app: FastAPI, profile: dict) -> SimpleNamespace:
    policy = load_production_policy(POLICY_PATH)
    workspace = SimpleNamespace(
        config=profile,
        plugins=SimpleNamespace(
            tool_registry=SimpleNamespace(
                names=lambda: sorted(policy.required_registered_tools)
            ),
            modes=[
                SimpleNamespace(name=name) for name in policy.required_registered_modes
            ],
        ),
    )
    agents = {"bank-assistant": workspace}

    async def get_agent(agent_id: str) -> object:
        return agents[agent_id]

    return SimpleNamespace(
        get_all_plugin_manifests=lambda: {"bank-runtime": {}},
        get_registered_channels=lambda: {"bank-runtime": object()},
        get_workspace_manager=lambda: SimpleNamespace(
            agents=agents,
            get_agent=get_agent,
        ),
        _plugin_http_app=app,
    )


def test_startup_guard_prunes_routes_and_publishes_ready(monkeypatch) -> None:
    monkeypatch.setenv("BANK_RUNTIME_PRODUCTION_GUARD", "1")
    profile = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    app = FastAPI()

    @app.get("/api/bank-runtime/health")
    async def bank_health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/creator/generate")
    async def creator() -> dict[str, str]:
        return {"status": "bad"}

    reset_production_readiness_for_tests()
    try:
        result = execute_production_guard(
            registry=_strict_registry(app, profile),
            importer=lambda _name: object(),
        )
        current = get_production_readiness()
    finally:
        reset_production_readiness_for_tests()

    assert result.ready is True
    assert current.ready is True
    assert TestClient(app).post("/api/creator/generate").status_code == 404


def test_startup_guard_rejects_reachable_shell_without_value_echo(
    monkeypatch,
) -> None:
    monkeypatch.setenv("BANK_RUNTIME_PRODUCTION_GUARD", "1")
    profile = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    profile["tools"]["builtin_tools"]["execute_shell_command"]["enabled"] = True
    app = FastAPI()

    @app.get("/api/bank-runtime/health")
    async def bank_health() -> dict[str, str]:
        return {"status": "ok"}

    reset_production_readiness_for_tests()
    try:
        with pytest.raises(RuntimeError) as error:
            execute_production_guard(
                registry=_strict_registry(app, profile),
                importer=lambda _name: object(),
            )
        current = get_production_readiness()
    finally:
        reset_production_readiness_for_tests()

    assert current.ready is False
    assert "forbidden_tool" in current.reason_codes
    assert "execute_shell_command" not in str(error.value)
