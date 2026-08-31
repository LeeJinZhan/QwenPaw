"""Fail-closed production surface policy for the Bank Runtime plugin."""

from __future__ import annotations

import importlib
import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from fastapi import HTTPException, status


_BANK_RUNTIME_REACHABLE_TOOLS = {
    "activate_personal_skill",
    "artifact_generate",
    "artifact_revise",
    "bank_assistant",
    "template_fill_docx",
}


@dataclass(frozen=True)
class ProductionPolicy:
    required_plugins: frozenset[str]
    required_plugin_channels: frozenset[str]
    allowed_agents: frozenset[str]
    required_registered_modes: frozenset[str]
    required_registered_tools: frozenset[str]
    allowed_reachable_tools: frozenset[str]
    allowed_enabled_channels: frozenset[str]
    allowed_route_prefixes: tuple[str, ...]
    forbidden_tools: frozenset[str]
    forbidden_features: frozenset[str]
    forbidden_harnesses: frozenset[str]


@dataclass(frozen=True)
class ProductionSnapshot:
    plugins: set[str]
    plugin_channels: set[str]
    loaded_agents: set[str]
    registered_modes: set[str]
    registered_tools: set[str]
    reachable_tools: set[str]
    enabled_channels: set[str]
    enabled_mcp_clients: set[str]
    enabled_harnesses: set[str]
    active_features: set[str]
    route_paths: set[str]


@dataclass(frozen=True)
class GuardResult:
    ready: bool
    reason_codes: tuple[str, ...] = ()

    def public_payload(self) -> str:
        """Return only stable reason codes, never offending values."""
        return json.dumps(
            {
                "ready": self.ready,
                "reason_codes": list(self.reason_codes),
            },
            sort_keys=True,
            separators=(",", ":"),
        )


_READINESS_LOCK = threading.Lock()
_READINESS = GuardResult(
    ready=False,
    reason_codes=("startup_guard_not_run",),
)


def production_guard_enabled() -> bool:
    return os.environ.get("BANK_RUNTIME_PRODUCTION_GUARD", "").strip() in {
        "1",
        "true",
        "TRUE",
        "yes",
        "YES",
    }


def publish_production_readiness(result: GuardResult) -> None:
    global _READINESS
    with _READINESS_LOCK:
        _READINESS = result


def get_production_readiness() -> GuardResult:
    with _READINESS_LOCK:
        return _READINESS


def reset_production_readiness_for_tests() -> None:
    publish_production_readiness(
        GuardResult(
            ready=False,
            reason_codes=("startup_guard_not_run",),
        )
    )


def require_production_readiness() -> None:
    """Reject strict-profile traffic until startup evaluation passes."""
    if not production_guard_enabled():
        return
    result = get_production_readiness()
    if result.ready:
        return
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={
            "code": "bank_runtime_not_ready",
            "reason_codes": list(result.reason_codes),
        },
    )


def _strings(value: Iterable[Any]) -> frozenset[str]:
    return frozenset(str(item) for item in value)


def load_production_policy(path: Path | None = None) -> ProductionPolicy:
    policy_path = path or Path(__file__).resolve().parents[1] / (
        "production-policy.json"
    )
    payload = json.loads(policy_path.read_text(encoding="utf-8"))
    approved = payload["approved_registry"]
    return ProductionPolicy(
        required_plugins=_strings(approved["required_plugins"]),
        required_plugin_channels=_strings(approved["required_plugin_channels"]),
        allowed_agents=_strings(approved["agents"]),
        required_registered_modes=_strings(approved["required_registered_modes"]),
        required_registered_tools=_strings(approved["required_registered_tools"]),
        allowed_reachable_tools=_strings(approved["reachable_tools"]),
        allowed_enabled_channels=_strings(approved["enabled_channels"]),
        allowed_route_prefixes=tuple(payload["http"]["allowed_prefixes"]),
        forbidden_tools=_strings(payload["denied"]["tools"]),
        forbidden_features=_strings(payload["denied"]["features"]),
        forbidden_harnesses=_strings(payload["denied"]["harnesses"]),
    )


def _route_is_allowed(path: str, policy: ProductionPolicy) -> bool:
    return any(
        path == prefix.rstrip("/") or path.startswith(prefix)
        for prefix in policy.allowed_route_prefixes
    )


def evaluate_snapshot(
    snapshot: ProductionSnapshot,
    policy: ProductionPolicy,
) -> GuardResult:
    """Compare one startup snapshot with the reviewed production policy."""
    reasons: set[str] = set()
    plugins = set(snapshot.plugins)
    channels = set(snapshot.plugin_channels)
    agents = set(snapshot.loaded_agents)
    modes = set(snapshot.registered_modes)
    tools = set(snapshot.registered_tools)
    reachable = set(snapshot.reachable_tools)

    if policy.required_plugins - plugins:
        reasons.add("missing_required_plugin")
    if policy.required_plugin_channels - channels:
        reasons.add("missing_required_channel")
    if agents - policy.allowed_agents:
        reasons.add("unknown_agent")
    if policy.allowed_agents - agents:
        reasons.add("missing_required_agent")
    if policy.required_registered_modes - modes:
        reasons.add("missing_registered_mode")
    if policy.required_registered_tools - tools:
        reasons.add("missing_registered_tool")
    if reachable & policy.forbidden_tools:
        reasons.add("forbidden_tool")
    if reachable - policy.allowed_reachable_tools:
        reasons.add("unapproved_reachable_tool")
    if set(snapshot.enabled_channels) - policy.allowed_enabled_channels:
        reasons.add("unapproved_enabled_channel")
    if set(snapshot.enabled_harnesses) & policy.forbidden_harnesses:
        reasons.add("forbidden_harness")
    if set(snapshot.enabled_harnesses) - policy.forbidden_harnesses:
        reasons.add("unapproved_harness")
    if set(snapshot.active_features) & policy.forbidden_features:
        reasons.add("forbidden_feature")
    if any(
        not _route_is_allowed(route_path, policy) for route_path in snapshot.route_paths
    ):
        reasons.add("unapproved_http_route")
    ordered = tuple(sorted(reasons))
    return GuardResult(ready=not ordered, reason_codes=ordered)


def apply_production_route_allowlist(
    app: Any,
    policy: ProductionPolicy,
    *,
    explicit_allowed_routes: Iterable[Any] = (),
) -> int:
    """Remove routes outside the bank ingress or its reviewed plugin owner."""
    routes = getattr(getattr(app, "router", None), "routes", None)
    if routes is None:
        raise RuntimeError("production_route_registry_unavailable")
    explicit_route_ids = {id(route) for route in explicit_allowed_routes}
    kept = []
    removed = 0
    for route in routes:
        path = str(getattr(route, "path", ""))
        if id(route) in explicit_route_ids or (
            path and _route_is_allowed(path, policy)
        ):
            kept.append(route)
        else:
            removed += 1
    routes[:] = kept
    if hasattr(app, "openapi_schema"):
        app.openapi_schema = None
    return removed


def apply_production_agent_allowlist(
    manager: Any,
    allowed_agents: Iterable[str],
) -> None:
    """Prevent QwenPaw from lazily starting an unreviewed Agent."""
    allowed = frozenset(str(agent_id) for agent_id in allowed_agents)
    if getattr(manager, "_bank_runtime_agent_allowlist_installed", False):
        return
    original_get_agent = getattr(manager, "get_agent", None)
    if not callable(original_get_agent):
        raise RuntimeError("production_agent_registry_unavailable")

    async def guarded_get_agent(agent_id: str, *args: Any, **kwargs: Any) -> Any:
        if str(agent_id) not in allowed:
            raise RuntimeError("production_agent_not_allowed")
        return await original_get_agent(agent_id, *args, **kwargs)

    setattr(manager, "get_agent", guarded_get_agent)
    setattr(manager, "_bank_runtime_agent_allowlist_installed", True)


def _enabled_names(mapping: Any) -> set[str]:
    if not isinstance(mapping, dict):
        return set()
    return {
        str(name)
        for name, config in mapping.items()
        if isinstance(config, dict) and config.get("enabled") is True
    }


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        dumped = dump(mode="python")
        return dumped if isinstance(dumped, dict) else {}
    return {}


def _workspace_values(manager: Any) -> list[Any]:
    agents = getattr(manager, "agents", None)
    if isinstance(agents, dict):
        return list(agents.values())
    hidden_agents = getattr(manager, "_agents", None)
    if isinstance(hidden_agents, dict):
        return list(hidden_agents.values())
    return []


def _workspace_ids(manager: Any) -> set[str]:
    agents = getattr(manager, "agents", None)
    if isinstance(agents, dict):
        return {str(agent_id) for agent_id in agents}
    hidden_agents = getattr(manager, "_agents", None)
    if isinstance(hidden_agents, dict):
        return {str(agent_id) for agent_id in hidden_agents}
    return set()


def _snapshot_workspace(
    workspace: Any,
    *,
    registered_modes: set[str],
    registered_tools: set[str],
    reachable_tools: set[str],
    enabled_channels: set[str],
    enabled_mcp_clients: set[str],
    enabled_harnesses: set[str],
    active_features: set[str],
) -> None:
    plugins = getattr(workspace, "plugins", None)
    for mode in getattr(plugins, "modes", ()):
        name = str(getattr(mode, "name", "") or "")
        if name:
            registered_modes.add(name)
    tool_registry = getattr(plugins, "tool_registry", None)
    names = getattr(tool_registry, "names", None)
    if callable(names):
        registered_tools.update(str(name) for name in names())

    profile = _mapping(
        getattr(workspace, "config", None) or getattr(workspace, "_config", None)
    )
    channels = _mapping(profile.get("channels"))
    enabled_channels.update(_enabled_names(channels))
    tools = _mapping(profile.get("tools"))
    reachable_tools.update(_enabled_names(_mapping(tools.get("builtin_tools"))))
    mcp = _mapping(profile.get("mcp"))
    enabled_mcp_clients.update(_enabled_names(_mapping(mcp.get("clients"))))
    acp = _mapping(profile.get("acp"))
    enabled_harnesses.update(_enabled_names(_mapping(acp.get("agents"))))

    browser = _mapping(profile.get("browser"))
    if browser.get("experimental") is True or any(
        browser.get(key)
        for key in ("cdp_url", "executable_path", "user_data_dir", "proxy")
    ):
        active_features.add("browser_use")
    coding_mode = _mapping(profile.get("coding_mode"))
    if coding_mode.get("enabled") is True:
        active_features.add("computer_use")
    running = _mapping(profile.get("running"))
    reme = _mapping(running.get("reme_light_memory_config"))
    auto_search = _mapping(reme.get("auto_memory_search_config"))
    if (
        reme.get("memory_search_enabled") is not False
        or bool(reme.get("auto_memory_interval"))
        or reme.get("dream_cron_enabled") is not False
        or auto_search.get("enabled") is not False
    ):
        active_features.add("long_term_memory")


def collect_registry_snapshot(registry: Any) -> ProductionSnapshot:
    """Read the loaded process surface without exposing configuration data."""
    manager = registry.get_workspace_manager()
    workspaces = _workspace_values(manager)
    registered_modes: set[str] = set()
    registered_tools: set[str] = set()
    reachable_tools: set[str] = set()
    enabled_channels: set[str] = set()
    enabled_mcp_clients: set[str] = set()
    enabled_harnesses: set[str] = set()
    active_features: set[str] = set()
    for workspace in workspaces:
        _snapshot_workspace(
            workspace,
            registered_modes=registered_modes,
            registered_tools=registered_tools,
            reachable_tools=reachable_tools,
            enabled_channels=enabled_channels,
            enabled_mcp_clients=enabled_mcp_clients,
            enabled_harnesses=enabled_harnesses,
            active_features=active_features,
        )
    app = getattr(registry, "_plugin_http_app", None)
    routes = getattr(getattr(app, "router", None), "routes", ())
    return ProductionSnapshot(
        plugins=set(registry.get_all_plugin_manifests()),
        plugin_channels=set(registry.get_registered_channels()),
        loaded_agents=_workspace_ids(manager),
        registered_modes=registered_modes,
        registered_tools=registered_tools,
        reachable_tools=reachable_tools,
        enabled_channels=enabled_channels,
        enabled_mcp_clients=enabled_mcp_clients,
        enabled_harnesses=enabled_harnesses,
        active_features=active_features,
        route_paths={
            str(getattr(route, "path", ""))
            for route in routes
            if getattr(route, "path", "")
        },
    )


def execute_production_guard(
    *,
    registry: Any | None = None,
    importer: Callable[[str], object] = importlib.import_module,
) -> GuardResult:
    """Prune routes, evaluate startup state, publish it, then fail closed."""
    if not production_guard_enabled():
        return get_production_readiness()
    try:
        if registry is None:
            from qwenpaw.plugins.registry import PluginRegistry

            registry = PluginRegistry()
        policy = load_production_policy()
        app = getattr(registry, "_plugin_http_app", None)
        get_http_registrations = getattr(
            registry,
            "get_http_router_registrations",
            None,
        )
        allowed_plugin_routes: list[Any] = []
        if callable(get_http_registrations):
            for registration in get_http_registrations():
                if str(getattr(registration, "plugin_id", "")) in (
                    policy.required_plugins
                ):
                    allowed_plugin_routes.extend(
                        getattr(registration, "routes", ()),
                    )
        apply_production_route_allowlist(
            app,
            policy,
            explicit_allowed_routes=allowed_plugin_routes,
        )
        snapshot = collect_registry_snapshot(registry)
        apply_production_agent_allowlist(
            registry.get_workspace_manager(),
            policy.allowed_agents,
        )
        snapshot_result = evaluate_snapshot(snapshot, policy)
        import_result = dependency_probe(importer=importer)
        reason_codes = tuple(
            sorted(set(snapshot_result.reason_codes) | set(import_result.reason_codes))
        )
        result = GuardResult(ready=not reason_codes, reason_codes=reason_codes)
    except Exception as exc:
        result = GuardResult(
            ready=False,
            reason_codes=("startup_snapshot_unavailable",),
        )
        publish_production_readiness(result)
        raise RuntimeError(
            "bank_runtime_production_guard_failed:startup_snapshot_unavailable"
        ) from exc
    publish_production_readiness(result)
    if not result.ready:
        raise RuntimeError(
            "bank_runtime_production_guard_failed:" + ",".join(result.reason_codes)
        )
    return result


def validate_production_agent_profile(profile: dict[str, Any]) -> GuardResult:
    """Validate the reviewed, credential-free production Agent template."""
    reasons: set[str] = set()
    channels = _enabled_names(profile.get("channels"))
    tools = _enabled_names(
        (profile.get("tools") or {}).get("builtin_tools")
        if isinstance(profile.get("tools"), dict)
        else None
    )
    harnesses = _enabled_names(
        (profile.get("acp") or {}).get("agents")
        if isinstance(profile.get("acp"), dict)
        else None
    )
    if channels != {"bank-runtime"}:
        reasons.add("unapproved_enabled_channel")
    if tools - _BANK_RUNTIME_REACHABLE_TOOLS:
        reasons.add("unapproved_reachable_tool")
    if harnesses:
        reasons.add("forbidden_harness")

    coding = profile.get("coding_mode") or {}
    if not isinstance(coding, dict) or coding.get("enabled") is not False:
        reasons.add("coding_mode_enabled")

    running = profile.get("running") or {}
    reme = running.get("reme_light_memory_config") or {}
    auto_search = reme.get("auto_memory_search_config") or {}
    if (
        reme.get("memory_search_enabled") is not False
        or bool(reme.get("auto_memory_interval"))
        or reme.get("dream_cron_enabled") is not False
        or auto_search.get("enabled") is not False
    ):
        reasons.add("long_term_memory_enabled")

    ordered = tuple(sorted(reasons))
    return GuardResult(ready=not ordered, reason_codes=ordered)


def validate_production_root_config(config: dict[str, Any]) -> GuardResult:
    """Validate root-only browser, plugin and global authority controls."""
    reasons: set[str] = set()
    browser = config.get("browser") or {}
    if not isinstance(browser, dict) or browser.get("experimental") is not False:
        reasons.add("browser_enabled")
    if isinstance(browser, dict) and any(
        browser.get(key)
        for key in ("cdp_url", "executable_path", "user_data_dir", "proxy")
    ):
        reasons.add("browser_authority_present")
    plugins = config.get("plugins") or {}
    if (
        not isinstance(plugins, dict)
        or not isinstance(plugins.get("bank-runtime"), dict)
        or plugins["bank-runtime"].get("enabled") is not True
    ):
        reasons.add("missing_required_plugin")
    if _enabled_names(_mapping(config.get("channels"))) != {"bank-runtime"}:
        reasons.add("unapproved_enabled_channel")
    tools = _mapping(config.get("tools"))
    if _enabled_names(_mapping(tools.get("builtin_tools"))) - _BANK_RUNTIME_REACHABLE_TOOLS:
        reasons.add("unapproved_reachable_tool")
    acp = _mapping(config.get("acp"))
    if _enabled_names(_mapping(acp.get("agents"))):
        reasons.add("forbidden_harness")
    agents = _mapping(config.get("agents"))
    profiles = _mapping(agents.get("profiles"))
    expected_profiles = {
        "bank-assistant": True,
        "default": False,
        "QwenPaw_QA_Agent_0.2": False,
    }
    actual_profiles = {
        name: _mapping(profile).get("enabled", True)
        for name, profile in profiles.items()
    }
    if (
        agents.get("active_agent") != "bank-assistant"
        or agents.get("agent_order") != ["bank-assistant"]
        or actual_profiles != expected_profiles
    ):
        reasons.add("unapproved_agent_profile")
    ordered = tuple(sorted(reasons))
    return GuardResult(ready=not ordered, reason_codes=ordered)


def dependency_probe(
    *,
    importer: Callable[[str], object] = importlib.import_module,
) -> GuardResult:
    """Probe required delivery imports without returning import errors."""
    categories = {
        "oss": ("oss2",),
        "pdf": ("pypdf",),
        "http": ("httpx",),
        "security": ("cryptography",),
        "office": ("zipfile", "xml.etree.ElementTree"),
    }
    reasons: list[str] = []
    for category, modules in categories.items():
        try:
            for module_name in modules:
                importer(module_name)
        except (ImportError, ModuleNotFoundError):
            reasons.append(f"missing_dependency_{category}")
    ordered = tuple(sorted(reasons))
    return GuardResult(ready=not ordered, reason_codes=ordered)


__all__ = [
    "GuardResult",
    "ProductionPolicy",
    "ProductionSnapshot",
    "apply_production_route_allowlist",
    "dependency_probe",
    "collect_registry_snapshot",
    "evaluate_snapshot",
    "execute_production_guard",
    "load_production_policy",
    "get_production_readiness",
    "production_guard_enabled",
    "publish_production_readiness",
    "require_production_readiness",
    "reset_production_readiness_for_tests",
    "validate_production_agent_profile",
    "validate_production_root_config",
]
