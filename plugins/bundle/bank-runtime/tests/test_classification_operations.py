from __future__ import annotations
import hashlib
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bank_runtime.classification_operations import (  # noqa: E402
    MappingRequest,
    build_job,
)
from bank_runtime.classification_mcp import (  # noqa: E402
    validate_runtime_url,
    mcp,
)


def test_cron_is_isolated_and_restricted():
    content = "test " * 20
    request = MappingRequest(
        mapping_version="v1",
        skill_content=content,
        skill_sha256=hashlib.sha256(content.encode()).hexdigest(),
        runtime_base_url="http://127.0.0.1:8000",
        runtime_tenant_id="test-tenant",
        operations_token="secret",
        enabled=True,
    )
    job = build_job(request)
    assert job.runtime.tool_safety is True
    assert job.runtime.share_session is False
    assert job.dispatch.silent is True
    assert job.request.request_context["subagent_allowed_tools"] == [
        "classification_operations__claim_tasks",
        "classification_operations__submit_classification",
    ]
    assert job.schedule.timezone == "Asia/Shanghai"
    assert "20" in job.request.input[0]["content"][0]["text"]
    request.interval_minutes = 7
    with pytest.raises(ValueError):
        build_job(request)


@pytest.mark.parametrize(
    "url",
    [
        "https://attacker.invalid",
        "http://localhost/foo",
        "http://user:pass@localhost",
        "file:///tmp/db",
        "http://localhost?token=x",
    ],
)
def test_runtime_url_is_fixed_and_approved(url):
    with pytest.raises(ValueError):
        validate_runtime_url(url)


@pytest.mark.asyncio
async def test_real_mcp_only_exposes_classification_tools():
    tools = await mcp.list_tools()
    assert {t.name for t in tools} == {"claim_tasks", "submit_classification"}
    assert "sql" not in str([t.inputSchema for t in tools])


@pytest.mark.asyncio
async def test_sdk_http_handshake_and_guarded_claim(tmp_path):
    import asyncio
    import json
    import os
    import socket
    import subprocess
    import threading
    import httpx
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    dict(
                        tenant_id="test-tenant",
                        agent_id="classification-operations",
                        mapping_version="v1",
                        skill_sha256="a" * 64,
                    )
                ).encode()
            )

        def do_POST(self):
            received.append(
                (
                    self.path,
                    self.headers.get("Authorization"),
                    json.loads(
                        self.rfile.read(int(self.headers["Content-Length"]))
                    ),
                )
            )
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"tasks":[]}')

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}/mcp"
    token = tmp_path / "token"
    token.write_text("dedicated-test-token")
    env = dict(
        os.environ,
        PYTHONPATH=str(Path(__file__).resolve().parents[4] / "src"),
        BANK_CLASSIFICATION_RUNTIME_URL=(
            "http://127.0.0.1:" f"{server.server_port}"
        ),
        BANK_CLASSIFICATION_TOKEN_FILE=str(token),
        BANK_CLASSIFICATION_MCP_URL=url,
        BANK_CLASSIFICATION_MCP_PORT=str(port),
    )
    process = subprocess.Popen(
        [sys.executable, "-m", "bank_runtime.classification_mcp"],
        cwd=str(Path(__file__).resolve().parents[1]),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    def client_factory(headers=None, timeout=None, auth=None):
        return httpx.AsyncClient(
            headers=headers, timeout=timeout or 30, auth=auth, trust_env=False
        )

    try:
        async with httpx.AsyncClient(trust_env=False) as client:
            for _ in range(600):
                try:
                    response = await client.post(url, json={})
                    if response.status_code == 401:
                        break
                except httpx.ConnectError:
                    if process.poll() is not None:
                        raise AssertionError(
                            "Authenticated HTTP MCP exited during startup"
                        )
                    await asyncio.sleep(0.1)
            else:
                raise AssertionError("Authenticated HTTP MCP did not start")
            assert (
                await client.get(url.removesuffix("/mcp") + "/health")
            ).status_code == 401
            health = await client.get(
                url.removesuffix("/mcp") + "/health",
                headers={"Authorization": "Bearer dedicated-test-token"},
            )
            assert health.json() == dict(
                runtime_url=("http://127.0.0.1:" f"{server.server_port}"),
                tenant_id="test-tenant",
                agent_id="classification-operations",
                mapping_version="v1",
                skill_sha256="a" * 64,
            )
            from bank_runtime.classification_operations import (
                verify_mapping_proof,
            )

            mapping = MappingRequest(
                mapping_version="v1",
                skill_content="test " * 20,
                skill_sha256="a" * 64,
                runtime_base_url=("http://127.0.0.1:" f"{server.server_port}"),
                runtime_tenant_id="test-tenant",
                mcp_url=url,
                operations_token="dedicated-test-token",
            )
            await verify_mapping_proof(mapping)
            mapping.runtime_tenant_id = "different-tenant"
            with pytest.raises(ValueError, match="mismatch"):
                await verify_mapping_proof(mapping)
            response = await client.post(
                url,
                headers={"Authorization": "Bearer wrong"},
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            )
            assert response.status_code == 401
        async with client_factory(
            headers={"Authorization": "Bearer dedicated-test-token"}
        ) as authorized_client, streamable_http_client(
            url, http_client=authorized_client
        ) as (
            read,
            write,
            _,
        ):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()
                assert {t.name for t in listed.tools} == {
                    "claim_tasks",
                    "submit_classification",
                }
                result = await session.call_tool("claim_tasks", {"limit": 3})
                assert not result.isError
                assert received == [
                    (
                        "/runtime/worker/classification-operations/claim",
                        "Bearer dedicated-test-token",
                        {"limit": 3},
                    )
                ]
                result = await session.call_tool(
                    "submit_classification",
                    dict(
                        task_id="task-1",
                        claim_token="claim-1",
                        mapping_version="v1",
                        skill_sha256="a" * 64,
                        scene="general_qa",
                        confidence=0.9,
                        reason_code="clear_intent",
                        model_id="test-model",
                    ),
                )
                assert not result.isError
                assert (
                    received[-1][0]
                    == "/runtime/worker/classification-operations/submit"
                )
                result = await session.call_tool("claim_tasks", {"limit": 99})
                assert result.isError
                assert len(received) == 2
    finally:
        process.terminate()
        process.wait(timeout=10)
        server.shutdown()
        server.server_close()


def test_sync_requires_service_identity_and_explicit_enable(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from bank_runtime.classification_operations import (
        build_classification_router,
    )

    content = "test " * 20
    body = dict(
        mapping_version="v1",
        skill_content=content,
        skill_sha256=hashlib.sha256(content.encode()).hexdigest(),
        runtime_base_url="http://127.0.0.1:8000",
        runtime_tenant_id="test-tenant",
        operations_token="secret",
    )
    monkeypatch.setenv("QWENPAW_SERVICE_TOKEN", "trusted")
    monkeypatch.delenv(
        "QWENPAW_CLASSIFICATION_OPERATIONS_ENABLED", raising=False
    )
    app = FastAPI()
    app.include_router(build_classification_router())
    with TestClient(app) as client:
        assert (
            client.post(
                "/classification-operations/sync", json=body
            ).status_code
            == 401
        )
        assert (
            client.post(
                "/classification-operations/sync",
                json=body,
                headers={
                    "Authorization": "Bearer trusted",
                    "X-Agent-Id": "other-agent",
                },
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/classification-operations/sync",
                json=body,
                headers={
                    "Authorization": "Bearer trusted",
                    "X-Agent-Id": "classification-operations",
                },
            ).status_code
            == 503
        )


@pytest.mark.asyncio
async def test_mapping_mismatch_prevents_native_writes(monkeypatch):
    from unittest.mock import Mock
    import bank_runtime.classification_operations as operations
    import qwenpaw.config.config as native_config
    import qwenpaw.config.utils as native_utils

    content = (
        "---\nname: bank-classification-operations\n"
        "description: classify completed tasks\n---\nClassification policy.\n"
    )
    request = MappingRequest(
        mapping_version="v1",
        skill_content=content,
        skill_sha256=hashlib.sha256(content.encode()).hexdigest(),
        runtime_base_url="http://127.0.0.1:8000",
        runtime_tenant_id="test-tenant",
        operations_token="secret",
    )

    async def wrong_proof(payload):
        raise ValueError("Classification MCP runtime or mapping mismatch")

    monkeypatch.setattr(operations, "verify_mapping_proof", wrong_proof)
    save_agent = Mock()
    save_global = Mock()
    monkeypatch.setattr(native_config, "save_agent_config", save_agent)
    monkeypatch.setattr(native_utils, "save_config", save_global)
    with pytest.raises(ValueError, match="mismatch"):
        await operations.sync_mapping(object(), request)
    save_agent.assert_not_called()
    save_global.assert_not_called()


def test_tenant_is_required_for_sync():
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="runtime_tenant_id"):
        MappingRequest(
            mapping_version="v1",
            skill_content="test " * 20,
            skill_sha256="a" * 64,
            runtime_base_url="http://127.0.0.1:8000",
            operations_token="secret",
        )


def test_native_skill_registry_mapping_roundtrip(tmp_path):
    from qwenpaw.agents.skill_system.workspace_service import SkillService
    from qwenpaw.agents.skill_system.store import read_skill_manifest
    from bank_runtime.classification_operations import SKILL_NAME

    service = SkillService(tmp_path)
    content = (
        "---\nname: bank-classification-operations\n"
        "description: classify completed tasks\n---\n"
        "Classify a task using approved categories.\n"
    )
    config = dict(
        runtime_mapping_version="mapping-1",
        skill_sha256=hashlib.sha256(content.encode()).hexdigest(),
        runtime_tenant_id="tenant-1",
    )
    assert (
        service.create_skill(SKILL_NAME, content, config=config) == SKILL_NAME
    )
    assert service.enable_skill(SKILL_NAME)["success"]
    assert service.set_skill_channels(SKILL_NAME, ["console"])
    entry = read_skill_manifest(tmp_path)["skills"][SKILL_NAME]
    assert entry["config"] == config
    assert entry["enabled"] is True
    assert entry["channels"] == ["console"]
    new_content = content + "Prefer task intent.\n"
    config["runtime_mapping_version"] = "mapping-2"
    config["skill_sha256"] = hashlib.sha256(new_content.encode()).hexdigest()
    assert service.save_skill(
        skill_name=SKILL_NAME, content=new_content, config=config
    )["success"]
    entry = read_skill_manifest(tmp_path)["skills"][SKILL_NAME]
    assert entry["config"] == config
    assert entry["enabled"] is True
    assert entry["channels"] == ["console"]
    assert (
        hashlib.sha256(
            (tmp_path / "skills" / SKILL_NAME / "SKILL.md")
            .read_text(encoding="utf-8")
            .encode()
        ).hexdigest()
        == config["skill_sha256"]
    )


def test_run_requires_identity_and_an_enabled_native_job(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from bank_runtime.classification_operations import (
        build_classification_router,
    )

    monkeypatch.setenv("QWENPAW_SERVICE_TOKEN", "trusted")
    monkeypatch.setenv("QWENPAW_CLASSIFICATION_OPERATIONS_ENABLED", "1")
    import bank_runtime.classification_operations as operations

    monkeypatch.setattr(
        operations,
        "classification_status",
        AsyncMock(
            return_value={
                "isolation_state": "verified",
                "execution_mode": "scheduled",
            }
        ),
    )
    cron = SimpleNamespace(
        get_job=AsyncMock(return_value=None), run_job=AsyncMock()
    )
    workspace = SimpleNamespace(cron_manager=cron)
    manager = SimpleNamespace(get_agent=AsyncMock(return_value=workspace))
    app = FastAPI()
    app.state.multi_agent_manager = manager
    app.include_router(build_classification_router())
    headers = {
        "Authorization": "Bearer trusted",
        "X-Agent-Id": "classification-operations",
    }
    with TestClient(app) as client:
        assert client.post("/classification-operations/run").status_code == 401
        assert (
            client.post(
                "/classification-operations/run",
                headers={**headers, "X-Agent-Id": "other"},
            ).status_code
            == 403
        )
        manager.get_agent.assert_not_called()
        assert (
            client.post(
                "/classification-operations/run", headers=headers
            ).status_code
            == 409
        )
        cron.run_job.assert_not_called()
        cron.get_job.return_value = SimpleNamespace(enabled=False)
        assert (
            client.post(
                "/classification-operations/run", headers=headers
            ).status_code
            == 409
        )
        cron.run_job.assert_not_called()
        cron.get_job.return_value = SimpleNamespace(enabled=True)
        assert (
            client.post(
                "/classification-operations/run", headers=headers
            ).json()["started"]
            is True
        )
        cron.run_job.assert_awaited_once_with("classification-operations")
        workspace.cron_manager = None
        assert (
            client.post(
                "/classification-operations/run", headers=headers
            ).status_code
            == 409
        )
