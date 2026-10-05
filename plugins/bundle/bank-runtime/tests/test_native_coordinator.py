"""The real outer coordinator must await permit-bound native jobs."""
import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from agentscope.message import ToolCallBlock, ToolResultState
from qwenpaw.tool_calls import ToolCoordinator

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bank_runtime.gateway.middleware import BankRuntimeGatewayMiddleware


@pytest.mark.asyncio
@pytest.mark.parametrize('offload', [False, True])
@pytest.mark.parametrize('name', ['execute_shell_command', 'read_file'])
async def test_native_job_outlives_foreground_window_and_returns_actual_result(offload, name):
    reports = []
    class Client:
        async def report_guard(self, *args): pass
        async def report_result(self, call, status, duration, error): reports.append(status)
    class Executor:
        sandbox_context = {'native_analysis_enabled': True, 'isolation_level': 'container',
            'expires_at': (datetime.now(timezone.utc) + timedelta(seconds=3)).isoformat()}
        async def execute(self, **kwargs):
            # Includes queue wait; command timeout must not limit queue time.
            await asyncio.sleep(.15)
            return {'exit_code': 0, 'stdout': 'actual result'} if name == 'execute_shell_command' else {'content': 'actual result'}
    gateway = BankRuntimeGatewayMiddleware(Client(), sandbox_executor=Executor(), native_analysis_enabled=True)
    coordinator = ToolCoordinator(offload_on_deadline=offload, cancel_grace_period_secs=.01)
    coordinator.hooks.register(name, default_timeout_secs=.1)
    raw = {'command': 'python scratch/run.py', 'timeout': .02} if name == 'execute_shell_command' else {'file_path': 'scratch/result.txt'}
    gateway.prepare(name, raw, {'tool_call_id': 'permit-call'})
    call = ToolCallBlock(id='model-call', name=name, input=json.dumps(raw))
    async def forbidden(**kwargs):
        raise AssertionError('host implementation must never run')
        yield
    async def admitted(**kwargs):
        async for item in gateway.on_acting(SimpleNamespace(), kwargs, forbidden): yield item
    try:
        items = [item async for item in coordinator.execute(call, admitted,
            session_id='session', agent_id='assistant', root_session_id='session')]
        assert len(items) == 1 and items[0].state == ToolResultState.SUCCESS
        assert 'actual result' in items[0].content[0].text
        assert reports == ['completed']
        assert not items[0].metadata.get('offloaded')
    finally:
        await coordinator.shutdown()


@pytest.mark.asyncio
async def test_native_deadline_is_task_bounded_and_preserves_earlier_cancellation():
    from qwenpaw.tool_calls import ToolCallContext, set_call_context, reset_call_context
    from bank_runtime.sandbox.executor import bind_native_job_deadline
    now = asyncio.get_running_loop().time()
    ctx = ToolCallContext('call', 'execute_shell_command', 'session', 'agent', 'session',
        now, now + .01, asyncio.Event(), kill_deadline=now + .05)
    token = set_call_context(ctx)
    try:
        bind_native_job_deadline({'native_analysis_enabled': True, 'isolation_level': 'container',
            'expires_at': (datetime.now(timezone.utc) + timedelta(seconds=2)).isoformat()})
        assert ctx.kill_deadline == now + .05
        assert ctx.offload_deadline is None
        assert ctx.deadline_changed_event.is_set()
        ctx.kill_deadline = None
        bind_native_job_deadline({'native_analysis_enabled': True, 'isolation_level': 'container',
            'expires_at': (datetime.now(timezone.utc) + timedelta(seconds=.1)).isoformat()})
        assert 0 < ctx.remaining() <= .1
    finally:
        reset_call_context(token)


@pytest.mark.asyncio
async def test_user_cancel_still_interrupts_native_wait():
    from qwenpaw.tool_calls import get_call_context
    from bank_runtime.sandbox.executor import bind_native_job_deadline
    entered = asyncio.Event()
    coordinator = ToolCoordinator(offload_on_deadline=False, cancel_grace_period_secs=.01)
    coordinator.hooks.register('execute_shell_command', default_timeout_secs=.1)
    async def waiting(**kwargs):
        bind_native_job_deadline({'native_analysis_enabled': True, 'isolation_level': 'container',
            'expires_at': (datetime.now(timezone.utc) + timedelta(seconds=2)).isoformat()})
        entered.set()
        await get_call_context().cancel_event.wait()
        await asyncio.sleep(1)
        yield
    async def consume():
        return [item async for item in coordinator.execute(
            ToolCallBlock(id='cancel-me', name='execute_shell_command', input='{}'), waiting,
            session_id='session', agent_id='assistant', root_session_id='session')]
    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert await coordinator.cancel('cancel-me', force=True)
        results = await asyncio.wait_for(task, 1)
        assert len(results) == 1 and results[0].state == ToolResultState.INTERRUPTED
    finally:
        await coordinator.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['oom', 'timeout', 'binary'])
async def test_native_failure_is_recoverable_and_next_script_can_complete(failure):
    from bank_runtime.sandbox.executor import _technical_rejection
    reports = []
    class Client:
        async def report_guard(self, *args): pass
        async def report_result(self, call, status, duration, error): reports.append(status)
    class Executor:
        sandbox_context = {'native_analysis_enabled': True, 'isolation_level': 'container',
            'expires_at': (datetime.now(timezone.utc) + timedelta(seconds=3)).isoformat()}
        executions = 0
        async def execute(self, **kwargs):
            self.executions += 1
            if self.executions == 1:
                if failure == 'binary':
                    class Response:
                        def json(self): return {'detail': {'code': 'WORKER_FAILED', 'message': 'secret',
                            'details': {'reason': 'binary_file_requires_parser', 'token': 'secret'}}}
                    raise _technical_rejection(Response())
                return {'exit_code': 137 if failure == 'oom' else 124,
                    'oom_killed': failure == 'oom', 'timed_out': failure == 'timeout',
                    'container_available': True, 'stderr': 'Killed' if failure == 'oom' else 'command timeout'}
            return {'exit_code': 0, 'stdout': 'corrected script result'}
    executor = Executor()
    gateway = BankRuntimeGatewayMiddleware(Client(), sandbox_executor=executor, native_analysis_enabled=True)
    coordinator = ToolCoordinator(offload_on_deadline=False, cancel_grace_period_secs=.01)
    coordinator.hooks.register('execute_shell_command', default_timeout_secs=.1)
    async def forbidden(**kwargs):
        raise AssertionError('host implementation must never run')
        yield
    async def admitted(**kwargs):
        async for item in gateway.on_acting(SimpleNamespace(), kwargs, forbidden): yield item
    try:
        for i, expected in enumerate([ToolResultState.ERROR, ToolResultState.SUCCESS]):
            raw = {'command': f'python scratch/attempt{i}.py'}
            gateway.prepare('execute_shell_command', raw, {'tool_call_id': f'permit{i}'})
            items = [item async for item in coordinator.execute(
                ToolCallBlock(id=f'model{i}', name='execute_shell_command', input=json.dumps(raw)),
                admitted, session_id='session', agent_id='assistant', root_session_id='session')]
            assert len(items) == 1 and items[0].state == expected
            if i == 0 and failure == 'binary':
                assert 'Python format reader' in items[0].content[0].text
                assert 'secret' not in items[0].content[0].text
        assert reports == ['failed', 'completed']
        assert executor.executions == 2
    finally:
        await coordinator.shutdown()
