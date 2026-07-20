import asyncio, importlib.util, os, sys, tempfile, types
from pathlib import Path

tmp = tempfile.mkdtemp(prefix="ops_observer_mw_test_")

# Write .env for config.py
plugin_dir = Path("/mnt/data/workplace/QwenPaw/plugins/qwenpaw-ops-observer")
env_file = plugin_dir / ".env"
backup = None
if env_file.exists():
    backup = env_file.read_text()
env_file.write_text(f"QWENPAW_WORKING_DIR={tmp}\n# OPS_OBSERVER_DB_URL=\n")
os.environ.pop("OPS_OBSERVER_DB_URL", None)
os.environ.pop("QWENPAW_WORKING_DIR", None)

_agentscope = types.ModuleType("agentscope")
_middleware = types.ModuleType("agentscope.middleware")
_middleware.MiddlewareBase = type("MiddlewareBase", (), {})
_event = types.ModuleType("agentscope.event")

class ThinkingBlockDeltaEvent:
    def __init__(self, delta=""): self.delta = delta
class TextBlockDeltaEvent:
    def __init__(self, delta=""): self.delta = delta
_event.ThinkingBlockDeltaEvent = ThinkingBlockDeltaEvent
_event.TextBlockDeltaEvent = TextBlockDeltaEvent

_tool = types.ModuleType("agentscope.tool")
class ToolResponse:
    def __init__(self, state="success"):
        self.state = type("S", (), {"value": state})()
_tool.ToolResponse = ToolResponse

sys.modules.update({
    "agentscope": _agentscope,
    "agentscope.middleware": _middleware,
    "agentscope.event": _event,
    "agentscope.tool": _tool,
})

PLUGIN_DIR = "/mnt/data/workplace/QwenPaw/plugins/qwenpaw-ops-observer"
spec = importlib.util.spec_from_file_location(
    "plugin_qwenpaw_ops_observer", os.path.join(PLUGIN_DIR, "__init__.py"),
    submodule_search_locations=[PLUGIN_DIR])
pkg = importlib.util.module_from_spec(spec)
sys.modules["plugin_qwenpaw_ops_observer"] = pkg
spec.loader.exec_module(pkg)

obs_spec = importlib.util.spec_from_file_location(
    "plugin_qwenpaw_ops_observer.observer", os.path.join(PLUGIN_DIR, "observer.py"),
    submodule_search_locations=[PLUGIN_DIR])
obs_mod = importlib.util.module_from_spec(obs_spec)
sys.modules["plugin_qwenpaw_ops_observer.observer"] = obs_mod
obs_spec.loader.exec_module(obs_mod)

from plugin_qwenpaw_ops_observer.storage import ObserverService
from plugin_qwenpaw_ops_observer.observer import OpsObserverMiddleware


class FakeCtx:
    agent_id = "agent-main"
    session_id = "session-xyz"
    request = types.SimpleNamespace(channel="console")


def ok_handler(events):
    async def h(**_kw):
        for e in events:
            yield e
    return h


def boom_handler():
    async def h(**_kw):
        yield ThinkingBlockDeltaEvent("partial")
        raise RuntimeError("boom")
    return h


async def consume(agen):
    async for _ in agen:
        pass


async def main():
    svc = ObserverService.from_environment({})
    await svc.start()
    mw = OpsObserverMiddleware(svc, FakeCtx())

    # llm call 1: success with thinking + text chunks
    await consume(mw.on_reasoning(None, {}, ok_handler([ThinkingBlockDeltaEvent("hmm"), TextBlockDeltaEvent("hi")])))
    # llm call 2: raises -> error status
    try:
        await consume(mw.on_reasoning(None, {}, boom_handler()))
        raise AssertionError("should have raised")
    except RuntimeError:
        pass
    # tool call 1: success
    await consume(mw.on_acting(None, {"tool_call": types.SimpleNamespace(name="Bash")}, ok_handler([ToolResponse("success")])))
    # tool call 2: error response
    await consume(mw.on_acting(None, {"tool_call": types.SimpleNamespace(name="Write")}, ok_handler([ToolResponse("error")])))
    # reply: success
    await consume(mw.on_reply(None, {}, ok_handler([])))

    await svc._queue.join()

    recent = await svc.recent_runs(5)
    assert len(recent["runs"]) == 1, recent
    run = recent["runs"][0]
    assert run["llm_call_count"] == 2, run
    assert run["tool_call_count"] == 2, run
    assert run["tool_error_count"] == 1, run
    assert run["status"] == "success"
    assert run["channel"] == "console"

    tools = await svc.stats_tools(24)
    by_name = {t["tool_name"]: t for t in tools["tools"]}
    assert by_name["Bash"]["errors"] == 0 and by_name["Write"]["errors"] == 1, tools

    llm = await svc.stats_llm(24)
    assert llm["calls"] == 2 and llm["errors"] == 1, llm
    assert llm["avg_ttft_ms"] is not None

    await svc.stop()
    print("ALL MIDDLEWARE TESTS PASSED")
    print("run:", run)
    print("llm:", llm)

asyncio.run(main())

# Restore .env file
if backup is not None:
    env_file.write_text(backup)
elif env_file.exists():
    env_file.unlink()
