"""Resource cleanup must survive later cancellation and earlier hook failures."""
import asyncio
from types import SimpleNamespace
import pytest
from agentscope.message import Msg
from qwenpaw.runtime.hooks import HookBase, HookRegistry, HookResult, HookAction
from qwenpaw.runtime.phases import Phase
from qwenpaw.runtime.runtime import Runtime


class CallbackHook(HookBase):
    def __init__(self, name, phase, callback, priority=100):
        self.name, self.phase, self.callback, self.priority = name, phase, callback, priority
    async def run(self, ctx):
        return await self.callback(ctx)


@pytest.mark.asyncio
@pytest.mark.parametrize('error', [RuntimeError('cleanup failed'), asyncio.CancelledError()])
async def test_finally_runs_later_cleanup_and_preserves_first_error(error):
    registry = HookRegistry()
    released = []
    async def fail(_): raise error
    async def release(_): released.append(True); return HookResult()
    registry.register(CallbackHook('failure', Phase.FINALLY, fail, 1))
    registry.register(CallbackHook('release', Phase.FINALLY, release, 1000))
    with pytest.raises(type(error)) as raised: await registry.run(Phase.FINALLY, SimpleNamespace())
    assert raised.value is error
    assert released == [True]


@pytest.mark.asyncio
async def test_finally_short_circuit_cannot_skip_resource_cleanup():
    registry = HookRegistry()
    released = []
    async def stop(_): return HookResult(action=HookAction.SHORT_CIRCUIT)
    async def release(_): released.append(True); return HookResult()
    registry.register(CallbackHook('stop', Phase.FINALLY, stop, 1))
    registry.register(CallbackHook('release', Phase.FINALLY, release, 1000))
    await registry.run(Phase.FINALLY, SimpleNamespace())
    assert released == [True]


def runtime_fixture(pre_dispatch, close, cleanup):
    registry=HookRegistry()
    registry.register(CallbackHook('dispatch',Phase.PRE_DISPATCH,pre_dispatch))
    registry.register(CallbackHook('cleanup',Phase.FINALLY,cleanup))
    workspace=SimpleNamespace(plugins=SimpleNamespace(hook_registry=registry),session=None)
    ctx=SimpleNamespace(session_id='session',agent=SimpleNamespace(close=close),extras={},error=None,workspace=workspace)
    runtime=Runtime(workspace=workspace,app_services=None)
    runtime._normalize=lambda request:request
    runtime._build_context=lambda _:ctx
    return runtime


@pytest.mark.asyncio
async def test_close_recancellation_still_runs_finally_hooks():
    released=[]
    async def dispatch(_): return HookResult(action=HookAction.SHORT_CIRCUIT,payload=Msg(name='assistant',role='assistant',content=[{'type':'text','text':'ok'}]))
    async def close(): raise asyncio.CancelledError()
    async def cleanup(_): released.append(True); return HookResult()
    runtime=runtime_fixture(dispatch,close,cleanup)
    with pytest.raises(asyncio.CancelledError):
        _=[item async for item in runtime.run(SimpleNamespace())]
    assert released == [True]


@pytest.mark.asyncio
async def test_cleanup_error_does_not_replace_original_model_error():
    original=ValueError('original failure')
    async def dispatch(_): raise original
    async def close(): pass
    async def cleanup(_): raise RuntimeError('cleanup failure')
    runtime=runtime_fixture(dispatch,close,cleanup)
    with pytest.raises(ValueError) as raised:
        _=[item async for item in runtime.run(SimpleNamespace())]
    assert raised.value is original


@pytest.mark.asyncio
async def test_non_finally_guard_failure_still_stops_later_hooks():
    registry=HookRegistry()
    invoked=[]
    async def deny(_): raise ValueError('guard rejected')
    async def later(_): invoked.append(True); return HookResult()
    registry.register(CallbackHook('guard',Phase.PRE_EXECUTE,deny,1))
    registry.register(CallbackHook('later',Phase.PRE_EXECUTE,later,1000))
    with pytest.raises(ValueError): await registry.run(Phase.PRE_EXECUTE,SimpleNamespace())
    assert invoked == []
