import asyncio, importlib.util, os, sys, tempfile, types
from pathlib import Path

tmp = tempfile.mkdtemp(prefix="ops_observer_router_test_")

# Write .env for config.py (mirrors real usage)
plugin_dir = Path("/mnt/data/workplace/QwenPaw/plugins/qwenpaw-ops-observer")
env_file = plugin_dir / ".env"
backup = None
if env_file.exists():
    backup = env_file.read_text()
env_file.write_text(f"QWENPAW_WORKING_DIR={tmp}\n# OPS_OBSERVER_DB_URL=\n")
os.environ.pop("OPS_OBSERVER_DB_URL", None)
os.environ.pop("QWENPAW_WORKING_DIR", None)

# Stub agentscope (not installed in this env; plugin runs inside QwenPaw host)
_agentscope = types.ModuleType("agentscope")
_middleware = types.ModuleType("agentscope.middleware")
_middleware.MiddlewareBase = type("MiddlewareBase", (), {})
_event = types.ModuleType("agentscope.event")
_event.ThinkingBlockDeltaEvent = type("ThinkingBlockDeltaEvent", (), {})
_event.TextBlockDeltaEvent = type("TextBlockDeltaEvent", (), {})
_tool = types.ModuleType("agentscope.tool")
_tool.ToolResponse = type("ToolResponse", (), {})
sys.modules.update({
    "agentscope": _agentscope,
    "agentscope.middleware": _middleware,
    "agentscope.event": _event,
    "agentscope.tool": _tool,
})

PLUGIN_DIR = str(plugin_dir)

# Load the plugin as a package, mirroring PluginLoader._load_backend_module
spec = importlib.util.spec_from_file_location(
    "plugin_qwenpaw_ops_observer",
    os.path.join(PLUGIN_DIR, "__init__.py"),
    submodule_search_locations=[PLUGIN_DIR],
)
pkg = importlib.util.module_from_spec(spec)
sys.modules["plugin_qwenpaw_ops_observer"] = pkg
spec.loader.exec_module(pkg)

backend_spec = importlib.util.spec_from_file_location(
    "plugin_qwenpaw_ops_observer.backend",
    os.path.join(PLUGIN_DIR, "backend.py"),
    submodule_search_locations=[PLUGIN_DIR],
)
backend = importlib.util.module_from_spec(backend_spec)
sys.modules["plugin_qwenpaw_ops_observer.backend"] = backend
backend_spec.loader.exec_module(backend)

assert hasattr(backend, "plugin"), "backend must export `plugin`"
assert hasattr(backend.plugin, "register"), "plugin must have register()"

from fastapi import FastAPI
from fastapi.testclient import TestClient
from plugin_qwenpaw_ops_observer.storage import ObserverService
from plugin_qwenpaw_ops_observer.router import build_router

async def main():
    svc = ObserverService.from_environment({})
    await svc.start()
    app = FastAPI()
    app.include_router(build_router(svc), prefix="/api/ops-observer")
    client = TestClient(app)

    # health
    r = client.get("/api/ops-observer/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"

    # events: no token -> 401
    r = client.post("/api/ops-observer/events", json={
        "event_type": "chat_opened", "occurred_at": "2026-07-17T10:00:00Z"})
    assert r.status_code == 401, r.status_code

    # events: valid token -> 202
    token = svc._event_token
    r = client.post("/api/ops-observer/events",
                    json={"event_type": "chat_opened", "occurred_at": "2026-07-17T10:00:00Z",
                          "event_key": "/chat", "extra_field": "nope"})
    assert r.status_code == 422, r.status_code  # extra=forbid
    r = client.post("/api/ops-observer/events",
                    json={"event_type": "chat_opened", "occurred_at": "2026-07-17T10:00:00Z",
                          "event_key": "/chat"},
                    headers={"X-Ops-Observer-Token": token})
    assert r.status_code == 202, (r.status_code, r.text)

    # events: bad type -> 400
    r = client.post("/api/ops-observer/events",
                    json={"event_type": "bad_type", "occurred_at": "2026-07-17T10:00:00Z"},
                    headers={"X-Ops-Observer-Token": token})
    assert r.status_code == 400, r.status_code

    # seed one run + tool + llm
    svc.enqueue_run_summary({
        "schema_version": 2, "run_id": "run-r001", "agent_id": "agent-main",
        "session_key": "s1", "channel": "console", "trigger_type": "chat",
        "started_at": "2026-07-17T10:00:00Z", "completed_at": "2026-07-17T10:00:01Z",
        "status": "success", "duration_ms": 1000, "llm_call_count": 1,
        "tool_call_count": 1, "tool_error_count": 0, "output_artifact_count": 0,
        "error_category": None, "config_ref": "config-qwenpaw-ops-observer"})
    svc.enqueue_tool_call({"run_id": "run-r001", "tool_seq": 1, "tool_name": "Bash",
                           "status": "success", "started_at": "2026-07-17T10:00:00Z", "duration_ms": 200})
    svc.enqueue_llm_call({"run_id": "run-r001", "call_seq": 1, "status": "success",
                          "started_at": "2026-07-17T10:00:00Z", "duration_ms": 800,
                          "ttft_ms": 150, "thinking_chunks": 3, "text_chunks": 2})
    await svc._queue.join()

    for path in ["/stats/overview", "/stats/timeseries", "/stats/tools",
                 "/stats/agents", "/stats/llm", "/stats/events", "/runs/recent"]:
        r = client.get(f"/api/ops-observer{path}?hours=24")
        assert r.status_code == 200, (path, r.status_code, r.text)

    ov = client.get("/api/ops-observer/stats/overview").json()
    assert ov["total_runs"] == 1 and ov["total_user_events"] == 1, ov
    ev = client.get("/api/ops-observer/stats/events").json()
    assert ev["events"][0]["event_type"] == "chat_opened"
    # hours validation: >720 rejected
    r = client.get("/api/ops-observer/stats/overview?hours=9999")
    assert r.status_code == 422, r.status_code

    await svc.stop()
    print("ALL ROUTER TESTS PASSED")

asyncio.run(main())

# Restore .env file
if backup is not None:
    env_file.write_text(backup)
elif env_file.exists():
    env_file.unlink()
