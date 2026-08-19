from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def _module():
    spec = importlib.util.spec_from_file_location(
        "bank_runtime_console_test", BACKEND / "main.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _client(module) -> TestClient:
    app = FastAPI()
    app.include_router(module.router, prefix="/api/bank-runtime-console")
    return TestClient(app)


@pytest.fixture
def runtime_env(monkeypatch):
    monkeypatch.setenv("QWENPAW_RUNTIME_CONSOLE_BASE_URL", "http://127.0.0.1:8765")
    monkeypatch.setenv("QWENPAW_RUNTIME_CONSOLE_APP_ID", "bank_user_console")
    monkeypatch.setenv("QWENPAW_RUNTIME_CONSOLE_APP_TOKEN", "server-only-secret")
    monkeypatch.setenv("QWENPAW_RUNTIME_CONSOLE_APP_SCOPES", "assistant:read")


def test_connect_rejects_browser_supplied_runtime_credentials(
    runtime_env, monkeypatch
) -> None:
    module = _module()

    async def probe(*_args, **_kwargs):
        return {"data": {"items": []}}

    monkeypatch.setattr(module, "_runtime_request", probe)
    client = _client(module)

    for forbidden in (
        {"app_token": "browser-secret"},
        {"runtime_password": "password"},
        {"runtime_user_token": "ordinary-user-token"},
    ):
        response = client.post(
            "/api/bank-runtime-console/connect",
            json={"user_id": "user_a", "org_id": "org_a", **forbidden},
        )
        assert response.status_code == 422
        assert "browser-secret" not in response.text
        assert "ordinary-user-token" not in response.text


@pytest.mark.parametrize(
    "missing_env",
    [
        "QWENPAW_RUNTIME_CONSOLE_BASE_URL",
        "QWENPAW_RUNTIME_CONSOLE_APP_ID",
        "QWENPAW_RUNTIME_CONSOLE_APP_TOKEN",
    ],
)
def test_server_fixed_runtime_configuration_is_required(
    runtime_env,
    monkeypatch,
    missing_env,
) -> None:
    module = _module()
    monkeypatch.delenv(missing_env)

    with pytest.raises(module.HTTPException) as exc_info:
        module._runtime_headers()

    assert exc_info.value.status_code == 503
    assert "secret" not in str(exc_info.value.detail).lower()


def test_two_external_identities_are_forwarded_independently(
    runtime_env, monkeypatch
) -> None:
    module = _module()
    calls = []

    async def request(method, path, *, external_identity):
        calls.append((method, path, dict(external_identity)))
        if path.endswith("conv_a") and external_identity["user_id"] != "user_a":
            raise module.HTTPException(status_code=404, detail="Conversation not found")
        return {"data": {"conversation_id": path.rsplit("/", 1)[-1]}}

    monkeypatch.setattr(module, "_runtime_request", request)
    client = _client(module)

    a = client.get(
        "/api/bank-runtime-console/conversations/conv_a",
        headers={
            "X-Runtime-External-User-Id": "user_a",
            "X-Runtime-External-Org-Id": "org_a",
        },
    )
    b_cross_scope = client.get(
        "/api/bank-runtime-console/conversations/conv_a",
        headers={
            "X-Runtime-External-User-Id": "user_b",
            "X-Runtime-External-Org-Id": "org_b",
        },
    )

    assert a.status_code == 200
    assert b_cross_scope.status_code == 404
    assert calls[0][2] == {"user_id": "user_a", "org_id": "org_a"}
    assert calls[1][2] == {"user_id": "user_b", "org_id": "org_b"}


def test_router_exposes_only_connect_and_read_operations() -> None:
    module = _module()
    routes = {
        (route.path, tuple(sorted(route.methods or ())))
        for route in module.router.routes
    }

    assert routes == {
        ("/connect", ("POST",)),
        ("/conversations", ("GET",)),
        ("/conversations/{conversation_id}", ("GET",)),
    }
