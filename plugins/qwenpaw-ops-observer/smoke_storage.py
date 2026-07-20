import asyncio, importlib.util, os, sys, tempfile
from pathlib import Path

tmp = tempfile.mkdtemp(prefix="ops_observer_test_")

# Write a .env file so config.py reads from it (mirrors real usage)
plugin_dir = Path("/mnt/data/workplace/QwenPaw/plugins/qwenpaw-ops-observer")
env_file = plugin_dir / ".env"
backup = None
if env_file.exists():
    backup = env_file.read_text()
env_file.write_text(f"QWENPAW_WORKING_DIR={tmp}\n# OPS_OBSERVER_DB_URL=\n")
os.environ.pop("OPS_OBSERVER_DB_URL", None)
os.environ.pop("QWENPAW_WORKING_DIR", None)

# Clear config cache and load modules as a package
for mod_name in [m for m in sys.modules if m.startswith("plugin_qwenpaw_ops_observer")]:
    del sys.modules[mod_name]
spec = importlib.util.spec_from_file_location(
    "plugin_qwenpaw_ops_observer", str(plugin_dir / "__init__.py"),
    submodule_search_locations=[str(plugin_dir)])
pkg = importlib.util.module_from_spec(spec)
sys.modules["plugin_qwenpaw_ops_observer"] = pkg
spec.loader.exec_module(pkg)

storage_spec = importlib.util.spec_from_file_location(
    "plugin_qwenpaw_ops_observer.storage", str(plugin_dir / "storage.py"),
    submodule_search_locations=[str(plugin_dir)])
storage = importlib.util.module_from_spec(storage_spec)
sys.modules["plugin_qwenpaw_ops_observer.storage"] = storage
storage_spec.loader.exec_module(storage)

async def main():
    svc = storage.ObserverService.from_environment({})
    await svc.start()

    svc.enqueue_run_summary({
        "schema_version": 2, "run_id": "run-test001", "agent_id": "agent-main",
        "session_key": "raw-session-abc", "channel": "console", "trigger_type": "chat",
        "started_at": "2026-07-17T10:00:00Z", "completed_at": "2026-07-17T10:00:03Z",
        "status": "success", "duration_ms": 3000, "llm_call_count": 2,
        "tool_call_count": 3, "tool_error_count": 1, "output_artifact_count": 1,
        "error_category": None, "config_ref": "config-qwenpaw-ops-observer",
    })
    svc.enqueue_run_summary({
        "schema_version": 2, "run_id": "run-test002", "agent_id": "agent-main",
        "session_key": "raw-session-abc", "channel": "dingtalk", "trigger_type": "chat",
        "started_at": "2026-07-17T10:05:00Z", "completed_at": "2026-07-17T10:05:02Z",
        "status": "error", "duration_ms": 2000, "llm_call_count": 1,
        "tool_call_count": 0, "tool_error_count": 0, "output_artifact_count": 0,
        "error_category": "execution_error", "config_ref": "config-qwenpaw-ops-observer",
    })
    svc.enqueue_tool_call({"run_id": "run-test001", "tool_seq": 1, "tool_name": "Bash",
                           "status": "success", "started_at": "2026-07-17T10:00:01Z", "duration_ms": 500})
    svc.enqueue_tool_call({"run_id": "run-test001", "tool_seq": 2, "tool_name": "Read",
                           "status": "denied", "started_at": "2026-07-17T10:00:02Z", "duration_ms": 100})
    svc.enqueue_llm_call({"run_id": "run-test001", "call_seq": 1, "status": "success",
                          "started_at": "2026-07-17T10:00:00Z", "duration_ms": 1200,
                          "ttft_ms": 300, "thinking_chunks": 10, "text_chunks": 5})
    assert svc.enqueue_user_event("chat_opened", "2026-07-17T10:00:00Z", "/chat") is True
    assert svc.enqueue_user_event("page_viewed", "2026-07-17T10:01:00Z", "/ops-observer") is True
    assert svc.enqueue_user_event("not_allowed_type", "2026-07-17T10:02:00Z") is False

    await svc._queue.join()

    overview = await svc.stats_overview(24)
    ts = await svc.stats_timeseries(24)
    tools = await svc.stats_tools(24)
    agents = await svc.stats_agents(24)
    llm = await svc.stats_llm(24)
    events = await svc.stats_events(24)
    recent = await svc.recent_runs(10)
    await svc.stop()

    assert overview["total_runs"] == 2, overview
    assert overview["success_runs"] == 1
    assert overview["success_rate"] == 0.5
    assert overview["total_tool_calls"] == 3
    assert overview["total_tool_errors"] == 1
    assert overview["total_llm_calls"] == 3
    assert overview["active_agents"] == 1
    assert overview["total_user_events"] == 2
    assert len(ts["buckets"]) == 1 and ts["buckets"][0]["runs"] == 2 and ts["buckets"][0]["errors"] == 1
    by_name = {t["tool_name"]: t for t in tools["tools"]}
    assert by_name["Bash"]["calls"] == 1 and by_name["Bash"]["errors"] == 0
    assert by_name["Read"]["calls"] == 1 and by_name["Read"]["errors"] == 1
    assert agents["agents"][0]["agent_id"] == "agent-main" and agents["agents"][0]["runs"] == 2
    assert llm["calls"] == 1 and llm["avg_ttft_ms"] == 300.0
    assert {e["event_type"] for e in events["events"]} == {"chat_opened", "page_viewed"}
    assert len(recent["runs"]) == 2 and recent["runs"][0]["run_id"] == "run-test002"
    row = [r for r in recent["runs"] if r["run_id"] == "run-test001"][0]
    assert row["channel"] == "console"

    db = Path(tmp) / "ops_observer" / "observer.sqlite3"
    assert db.exists(), db
    js = Path(tmp) / "run_summaries" / "run-test001.json"
    assert js.exists(), js
    import json
    saved = json.loads(js.read_text())
    assert saved["session_key"].startswith("sess-"), saved["session_key"]
    assert saved["schema_version"] == 2
    print("ALL SMOKE TESTS PASSED")
    print("overview:", overview)
    print("timeseries buckets:", ts["buckets"])
    print("tools:", tools["tools"])
    print("llm:", llm)

asyncio.run(main())

# Restore .env file
if backup is not None:
    env_file.write_text(backup)
elif env_file.exists():
    env_file.unlink()
