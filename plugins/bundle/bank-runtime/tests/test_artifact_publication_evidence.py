"""A successful RPC is not proof that a downloadable artifact was published."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from agentscope.message import ToolCallBlock, ToolResultState
from agentscope.permission import PermissionBehavior

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bank_runtime.artifact_tools import FileOperationsIncompleteError
from bank_runtime.gateway.middleware import BankRuntimeGatewayMiddleware, GatewayPermissionEngine
from test_gateway_middleware import _Client, _DelegateEngine


async def invoke(middleware, payload):
    engine = GatewayPermissionEngine(_DelegateEngine(PermissionBehavior.ALLOW, middleware.client.events), middleware)
    assert (await engine.check_permission(SimpleNamespace(name='artifact_generate'), payload)).behavior == PermissionBehavior.ALLOW
    async def forbidden(**kwargs):
        pytest.fail('artifact must execute through Runtime')
        yield
    return [item async for item in middleware.on_acting(None, {'tool_call': ToolCallBlock(
        id='call-sql', name='artifact_generate', input=json.dumps(payload))}, forbidden)]


@pytest.mark.asyncio
@pytest.mark.parametrize('ids', [None, [], '', 'gfile-1', [None], [1], [' '], ['x', 'x'], [' x ']])
async def test_success_without_valid_publication_is_failed_and_keeps_completion_blocked(ids):
    class Client(_Client):
        async def execute_runtime_tool(self, *args):
            return {'status': 'success', 'result': {'artifact_status': 'succeeded', 'generated_file_ids': ids}}
    middleware = BankRuntimeGatewayMiddleware(Client(), native_analysis_enabled=True)
    result = await invoke(middleware, {'artifact_type': 'sql', 'title': '查询', 'output_name': 'query.sql', 'content': 'SELECT 1;'})
    assert result[-1].state == ToolResultState.ERROR
    with pytest.raises(FileOperationsIncompleteError):
        middleware._check_file_completion()


@pytest.mark.asyncio
async def test_one_success_does_not_hide_another_missing_file():
    class Client(_Client):
        async def execute_runtime_tool(self, preflight, name, payload):
            return {'status': 'success', 'result': {'artifact_status': 'succeeded',
                'generated_file_ids': ['gfile-html'] if payload['artifact_type'] == 'html' else []}}
    middleware = BankRuntimeGatewayMiddleware(Client(), native_analysis_enabled=True)
    for kind, content in [('sql', 'SELECT 1;'), ('html', '<p>报告</p>')]:
        await invoke(middleware, {'artifact_type': kind, 'title': '报告', 'output_name': f'report.{kind}', 'content': content})
    with pytest.raises(FileOperationsIncompleteError):
        middleware._check_file_completion()


@pytest.mark.asyncio
async def test_published_sql_clears_its_own_delivery_and_plain_analysis_has_no_requirement():
    class Client(_Client):
        async def execute_runtime_tool(self, *args):
            return {'status': 'success', 'result': {'artifact_status': 'succeeded', 'generated_file_ids': ['gfile-sql']}}
    middleware = BankRuntimeGatewayMiddleware(Client(), native_analysis_enabled=True)
    middleware._check_file_completion()
    result = await invoke(middleware, {'artifact_type': 'sql', 'title': '查询', 'content': 'SELECT 1;'})
    assert result[-1].state == ToolResultState.SUCCESS
    assert not middleware.unresolved_file_operations
    middleware._check_file_completion()
