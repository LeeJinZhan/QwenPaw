import sys
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from bank_runtime.management_auth import install_native_auth_bridge
from qwenpaw.app import auth


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(auth, "is_auth_enabled", lambda: True)
    monkeypatch.setattr(auth, "has_registered_users", lambda: True)
    monkeypatch.setattr(
        auth,
        "verify_token",
        lambda token: "admin" if token == "native-session" else None,
    )
    monkeypatch.setenv("QWENPAW_SERVICE_TOKEN", "runtime-service")
    app = FastAPI()
    app.add_middleware(auth.AuthMiddleware)
    install_native_auth_bridge(app)

    @app.api_route("/api/{path:path}", methods=["GET", "POST"])
    def response(path: str):
        return {"ok": True}

    with TestClient(app) as value:
        yield value


def test_service_token_cannot_authenticate_management_routes(client):
    headers = {
        "Authorization": "Bearer runtime-service",
        "X-Agent-Id": "bank-assistant",
    }
    assert (
        client.get(
            "/api/bank-runtime/agents/bank-assistant/health", headers=headers
        ).status_code
        == 200
    )
    for path in (
        "/api/models",
        "/api/agents",
        "/api/bank-runtime-evil/health",
        "/api/bank-runtime/admin",
        "/api/bank-runtime/agents/other/health",
    ):
        assert client.get(path, headers=headers).status_code == 401
    assert (
        client.get(
            "/api/models", headers={"Authorization": "Bearer native-session"}
        ).status_code
        == 200
    )


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong", "X-Agent-Id": "bank-assistant"},
        {"Authorization": "Bearer runtime-service"},
    ],
)
def test_service_route_requires_both_token_and_agent(client, headers):
    assert client.get("/api/bank-runtime/health", headers=headers).status_code == 401


def test_disabled_or_missing_native_account_blocks_all_access(client, monkeypatch):
    monkeypatch.setattr(auth, "has_registered_users", lambda: False)
    assert (
        client.get(
            "/api/models", headers={"Authorization": "Bearer native-session"}
        ).status_code
        == 503
    )
    assert (
        client.get(
            "/api/bank-runtime/health",
            headers={
                "Authorization": "Bearer runtime-service",
                "X-Agent-Id": "bank-assistant",
            },
        ).status_code
        == 503
    )
    monkeypatch.setattr(auth, "has_registered_users", lambda: True)
    monkeypatch.setattr(auth, "is_auth_enabled", lambda: False)
    assert client.get("/api/models").status_code == 503


def test_public_reads_do_not_allow_anonymous_management_writes(client):
    assert client.get("/api/settings/language").status_code == 200
    assert client.post("/api/settings/language").status_code == 401
    assert client.post("/api/auth/register").status_code == 401
    assert client.post("/api/desktop/shutdown").status_code == 401
    assert client.post("/api/auth/login").status_code == 200


def test_native_admin_token_does_not_replace_runtime_identity(monkeypatch):
    from bank_runtime.router import build_ingress_router

    monkeypatch.setattr(auth, "is_auth_enabled", lambda: True)
    monkeypatch.setattr(auth, "has_registered_users", lambda: True)
    monkeypatch.setattr(auth, "verify_token", lambda token: "admin")
    monkeypatch.setenv("QWENPAW_SERVICE_TOKEN", "runtime-service")
    monkeypatch.delenv("BANK_RUNTIME_PRODUCTION_GUARD", raising=False)
    app = FastAPI()
    app.add_middleware(auth.AuthMiddleware)
    install_native_auth_bridge(app)
    app.include_router(build_ingress_router(), prefix="/api/bank-runtime")
    with TestClient(app) as value:
        response = value.get(
            "/api/bank-runtime/health",
            headers={
                "Authorization": "Bearer native-session",
                "X-Agent-Id": "bank-assistant",
            },
        )
        assert response.status_code == 401


def test_native_router_allowlist_keeps_models_without_exposing_other_actions():
    from bank_runtime.production_guard import (
        apply_production_route_allowlist,
        load_production_policy,
    )
    from qwenpaw.app.routers.auth import router as auth_router
    from qwenpaw.app.routers.providers import router as providers_router
    from qwenpaw.app.routers.agents import router as agents_router

    app = FastAPI()
    app.include_router(auth_router, prefix="/api")
    app.include_router(providers_router, prefix="/api")
    app.include_router(providers_router, prefix="/api/agents/{agentId}")
    app.include_router(agents_router, prefix="/api")

    @app.post("/api/models/download")
    def download():
        pass

    @app.post("/")
    def arbitrary_root_write():
        pass

    apply_production_route_allowlist(app, load_production_policy())
    from fastapi.routing import iter_route_contexts

    routes = {
        (route.path, method)
        for route in iter_route_contexts(app.routes)
        for method in (route.methods or ())
    }
    assert ("/api/auth/login", "POST") in routes
    assert ("/api/auth/register", "POST") not in routes
    assert ("/api/models/custom-providers", "POST") in routes
    assert ("/api/agents/{agentId}/models/active", "PUT") in routes
    assert ("/api/agents", "GET") in routes
    assert ("/api/agents", "POST") not in routes
    assert ("/api/agents/{agentId}", "PUT") not in routes
    assert ("/api/models/download", "POST") not in routes
    assert ("/", "POST") not in routes


def test_pruned_nested_router_preserves_scopes_dependencies_and_http_behavior():
    from fastapi import APIRouter, Depends
    from bank_runtime.production_guard import (
        apply_production_route_allowlist,
        load_production_policy,
    )

    calls = []

    def dependency():
        calls.append("authorized")

    models = APIRouter(prefix="/models")

    @models.get("/active")
    def active(agentId: str = "root"):
        return {"agent": agentId}

    @models.post("/download")
    def download():
        return {"unexpected": True}

    group = APIRouter()
    group.include_router(models, dependencies=[Depends(dependency)])
    app = FastAPI()
    app.include_router(group, prefix="/api")
    app.include_router(group, prefix="/api/agents/{agentId}")
    apply_production_route_allowlist(app, load_production_policy())
    with TestClient(app) as value:
        assert value.get("/api/models/active").json() == {"agent": "root"}
        assert value.get("/api/agents/bank-assistant/models/active").json() == {
            "agent": "bank-assistant"
        }
        assert value.post("/api/models/download").status_code == 404
        assert (
            value.post("/api/agents/bank-assistant/models/download").status_code == 404
        )
    assert calls == ["authorized", "authorized"]
    assert any(route.path == "/models/download" for route in models.routes)
