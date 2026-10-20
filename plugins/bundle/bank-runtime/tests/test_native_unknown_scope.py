import json
from types import SimpleNamespace

import pytest
from agentscope.message import ToolCallBlock
from agentscope.permission import PermissionBehavior
from test_gateway_middleware import _Client, _DelegateEngine
from bank_runtime.gateway.middleware import BankRuntimeGatewayMiddleware, GatewayPermissionEngine
from bank_runtime.sandbox.executor import SandboxExecutionUnknown, SandboxExecutorError


class Executor:
    def __init__(self, error):
        self.error = error
        self.sandbox_context = {'task_scope_id': 'scope-a', 'sandbox_instance_id': 'sandbox-a'}

    async def execute(self, **kwargs):
        raise self.error


async def fail_first(error):
    client = _Client()
    middleware = BankRuntimeGatewayMiddleware(client, sandbox_executor=Executor(error), native_analysis_enabled=True)
    engine = GatewayPermissionEngine(_DelegateEngine(PermissionBehavior.ALLOW, client.events), middleware)
    decision = await engine.check_permission(SimpleNamespace(name='execute_shell_command'), {'command': 'first'})
    assert decision.behavior == PermissionBehavior.ALLOW
    async def local(**kwargs):
        raise AssertionError('No local fallback')
        yield
    with pytest.raises(Exception):
        async for _ in middleware.on_acting(SimpleNamespace(), {'tool_call': ToolCallBlock(
                id='first', name='execute_shell_command', input=json.dumps({'command': 'first'}))}, local):
            pass
    return middleware, engine, client


@pytest.mark.asyncio
@pytest.mark.parametrize('name,payload', [('execute_shell_command', {'command': 'changed'}),
                                        ('write_file', {'file_path': 'work/new.txt', 'content': 'changed'})])
async def test_unknown_blocks_changed_execution_in_same_sandbox(name, payload):
    middleware, engine, client = await fail_first(SandboxExecutionUnknown())
    count = len(client.events)
    assert (await engine.check_permission(SimpleNamespace(name=name), payload)).behavior == PermissionBehavior.DENY
    assert len(client.events) == count
    assert (await engine.check_permission(SimpleNamespace(name='policy_search'), {'query': 'independent'})).behavior == PermissionBehavior.ALLOW
    middleware.sandbox_executor.sandbox_context = {'task_scope_id': 'scope-b', 'sandbox_instance_id': 'sandbox-b'}
    assert (await engine.check_permission(SimpleNamespace(name=name), payload)).behavior == PermissionBehavior.ALLOW


@pytest.mark.asyncio
async def test_known_argument_error_allows_correction():
    _, engine, _ = await fail_first(SandboxExecutorError('Invalid argument', argument_reason='invalid_payload'))
    assert (await engine.check_permission(SimpleNamespace(name='execute_shell_command'), {'command': 'corrected'})).behavior == PermissionBehavior.ALLOW


@pytest.mark.asyncio
async def test_unknown_removes_only_affected_execution_schemas():
    middleware, _, _ = await fail_first(SandboxExecutionUnknown())
    captured = {}
    async def model(**kwargs):
        captured.update(kwargs)
        return object()
    tools = [{'type': 'function', 'function': {'name': name}} for name in
             ('execute_shell_command', 'write_file', 'policy_search', 'chart_generate')]
    await middleware.on_model_call(SimpleNamespace(), {'tools': tools, 'messages': []}, model)
    assert [item['function']['name'] for item in captured['tools']] == ['policy_search', 'chart_generate']
