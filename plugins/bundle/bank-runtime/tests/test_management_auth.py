import sys
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from bank_runtime.management_auth import install_native_auth_bridge
from qwenpaw.app import auth


@pytest.fixture
def agent_profiles(monkeypatch):
    profiles = {
        agent_id: SimpleNamespace(id=agent_id, enabled=True)
        for agent_id in ("bank-assistant", "assistant-a", "assistant_b")
    }
    profiles["disabled-agent"] = SimpleNamespace(id="disabled-agent", enabled=False)
    monkeypatch.setattr(
        "qwenpaw.config.utils.load_config",
        lambda: SimpleNamespace(agents=SimpleNamespace(profiles=profiles)),
    )
    return profiles


@pytest.fixture
def client(monkeypatch, agent_profiles):
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


@pytest.mark.parametrize("agent_id", ["bank-assistant", "assistant-a", "assistant_b"])
@pytest.mark.parametrize(
    "method, path",
    [
        ("GET", "/api/bank-runtime/agents/{agent_id}/health"),
        ("POST", "/api/bank-runtime/agents/{agent_id}/chat"),
        ("POST", "/api/bank-runtime/agents/{agent_id}/chat/stop"),
        ("GET", "/api/bank-runtime/health"),
        ("GET", "/api/bank-runtime/capabilities"),
    ],
)
def test_service_identity_accepts_each_enabled_agent(client, agent_id, method, path):
    response = client.request(
        method,
        path.format(agent_id=agent_id),
        headers={
            "Authorization": "Bearer runtime-service",
            "X-Agent-Id": agent_id,
        },
    )
    assert response.status_code == 200


@pytest.mark.parametrize("agent_id", ["missing-agent", "disabled-agent"])
@pytest.mark.parametrize(
    "path", ["/api/bank-runtime/agents/{agent_id}/health", "/api/bank-runtime/health"]
)
def test_service_identity_rejects_missing_or_disabled_agent(client, agent_id, path):
    response = client.get(
        path.format(agent_id=agent_id),
        headers={"Authorization": "Bearer runtime-service", "X-Agent-Id": agent_id},
    )
    assert response.status_code == 401


@pytest.mark.parametrize("agent_id", ["bank-assistant", "assistant-a"])
def test_service_identity_observes_agent_disable_on_next_request(
    client, agent_profiles, agent_id
):
    headers = {"Authorization": "Bearer runtime-service", "X-Agent-Id": agent_id}
    path = f"/api/bank-runtime/agents/{agent_id}/health"
    assert client.get(path, headers=headers).status_code == 200
    agent_profiles[agent_id].enabled = False
    assert client.get(path, headers=headers).status_code == 401


def test_service_identity_rejects_removed_default_agent(client, agent_profiles):
    del agent_profiles["bank-assistant"]
    response = client.get(
        "/api/bank-runtime/agents/bank-assistant/health",
        headers={
            "Authorization": "Bearer runtime-service",
            "X-Agent-Id": "bank-assistant",
        },
    )
    assert response.status_code == 401


def test_service_identity_rejects_mismatched_registry_id(client, agent_profiles):
    agent_profiles["bank-assistant"].id = "another-agent"
    response = client.get(
        "/api/bank-runtime/agents/bank-assistant/health",
        headers={
            "Authorization": "Bearer runtime-service",
            "X-Agent-Id": "bank-assistant",
        },
    )
    assert response.status_code == 401


@pytest.mark.parametrize("failure", [OSError, ValueError, RuntimeError])
def test_service_identity_fails_closed_when_registry_unavailable(
    client, monkeypatch, failure
):
    def unavailable():
        raise failure("private-registry-details")

    monkeypatch.setattr("qwenpaw.config.utils.load_config", unavailable)
    response = client.get(
        "/api/bank-runtime/agents/bank-assistant/health",
        headers={
            "Authorization": "Bearer runtime-service",
            "X-Agent-Id": "bank-assistant",
        },
    )
    assert response.status_code == 401
    assert "private-registry-details" not in response.text


@pytest.mark.parametrize(
    "method, path",
    [
        ("GET", "/api/bank-runtime/agents/assistant_b/health"),
        ("POST", "/api/bank-runtime/agents/assistant_b/chat"),
        ("POST", "/api/bank-runtime/agents/assistant_b/chat/stop"),
        ("POST", "/api/bank-runtime/agents/assistant-a/health"),
        ("GET", "/api/bank-runtime/agents/assistant-a/chat"),
        ("GET", "/api/agents/assistant-a/models/active"),
        ("POST", "/api/agents"),
        ("GET", "/api/bank-runtime/agents/assistant-a/admin"),
    ],
)
def test_non_default_agent_identity_stays_scoped(client, method, path):
    response = client.request(
        method,
        path,
        headers={
            "Authorization": "Bearer runtime-service",
            "X-Agent-Id": "assistant-a",
        },
    )
    assert response.status_code == 401


def test_non_default_agent_rejects_wrong_service_token(client):
    response = client.get(
        "/api/bank-runtime/agents/assistant-a/health",
        headers={"Authorization": "Bearer wrong", "X-Agent-Id": "assistant-a"},
    )
    assert response.status_code == 401


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


@pytest.mark.parametrize("agent_id", ["bank-assistant", "assistant-a"])
def test_native_admin_token_does_not_replace_runtime_identity(monkeypatch, agent_id):
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
                "X-Agent-Id": agent_id,
            },
        )
        assert response.status_code == 401


def test_non_default_agents_chat_and_stop_resolve_their_own_workspaces(
    monkeypatch, agent_profiles
):
    import json
    from bank_runtime.router import build_ingress_router
    from qwenpaw.app.task_tracker import TaskTracker
    from qwenpaw.runtime.hooks import HookRegistry

    monkeypatch.setattr(auth, "is_auth_enabled", lambda: True)
    monkeypatch.setattr(auth, "has_registered_users", lambda: True)
    monkeypatch.setattr(auth, "verify_token", lambda token: None)
    monkeypatch.setenv("QWENPAW_SERVICE_TOKEN", "runtime-service")
    monkeypatch.delenv("BANK_RUNTIME_PRODUCTION_GUARD", raising=False)
    monkeypatch.setattr(
        "qwenpaw.app.agent_context.load_config",
        lambda: SimpleNamespace(agents=SimpleNamespace(profiles=agent_profiles)),
    )

    class Channel:
        def __init__(self, agent_id):
            self.agent_id = agent_id
            self.requests = []

        async def stream_one(self, request):
            self.requests.append(request)
            yield "data: " + json.dumps(
                {
                    "object": "message",
                    "id": "answer",
                    "type": "message",
                    "status": "completed",
                    "content": self.agent_id,
                }
            ) + "\n\n"
            yield 'data: {"object":"response","status":"completed"}\n\n'

        async def get_channel(self, name):
            assert name == "bank-runtime"
            return self

    class Chats:
        def __init__(self, agent_id):
            self.chat_id = agent_id + ":session"
            self.stops = []

        async def get_or_create_chat(self, session_id, user_id, channel):
            assert (session_id, user_id, channel) == (
                "same-session",
                "user-001",
                "bank-runtime",
            )
            return SimpleNamespace(id=self.chat_id)

        async def get_chat_id_by_session(self, *, session_id, channel):
            self.stops.append((session_id, channel))
            return self.chat_id

    workspaces = {
        agent_id: SimpleNamespace(
            channel_manager=Channel(agent_id),
            chat_manager=Chats(agent_id),
            task_tracker=TaskTracker(),
            plugins=SimpleNamespace(hook_registry=HookRegistry()),
        )
        for agent_id in ("assistant-a", "assistant_b")
    }
    selected = []

    async def get_agent(agent_id):
        selected.append(agent_id)
        return workspaces[agent_id]

    app = FastAPI()
    app.add_middleware(auth.AuthMiddleware)
    install_native_auth_bridge(app)
    app.state.multi_agent_manager = SimpleNamespace(get_agent=get_agent)
    app.include_router(build_ingress_router(), prefix="/api/bank-runtime")
    with TestClient(app) as value:
        for agent_id in workspaces:
            response = value.post(
                f"/api/bank-runtime/agents/{agent_id}/chat",
                headers={
                    "Authorization": "Bearer runtime-service",
                    "X-Agent-Id": agent_id,
                },
                json={
                    "runtime_task_id": "task-" + agent_id,
                    "session_id": "same-session",
                    "user_id": "user-001",
                    "channel": "bank-runtime",
                    "input": [
                        {"role": "user", "content": [{"type": "text", "text": "hello"}]}
                    ],
                },
            )
            assert response.status_code == 200
            events = [
                json.loads(line[6:])
                for line in response.text.splitlines()
                if line.startswith("data: ")
            ]
            assert any(
                event.get("event") == "answer.chunk" and event.get("text") == agent_id
                for event in events
            )
            assert any(event.get("event") == "answer.completed" for event in events)
        response = value.post(
            "/api/bank-runtime/agents/assistant-a/chat/stop?chat_id=same-session",
            headers={
                "Authorization": "Bearer runtime-service",
                "X-Agent-Id": "assistant-a",
            },
            json={"runtime_task_id": "task-assistant-a", "session_id": "same-session"},
        )
        assert response.status_code == 200
    assert selected == ["assistant-a", "assistant_b", "assistant-a"]
    assert len(workspaces["assistant-a"].channel_manager.requests) == 1
    assert len(workspaces["assistant_b"].channel_manager.requests) == 1
    assert workspaces["assistant-a"].chat_manager.stops == [
        ("same-session", "bank-runtime")
    ]
    assert workspaces["assistant_b"].chat_manager.stops == []


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
