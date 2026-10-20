import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
from agentscope.permission import PermissionBehavior

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from bank_runtime.gateway.middleware import BankRuntimeGatewayMiddleware, _sandbox_tool_response
from bank_runtime.session import _sanitize_agent_state


def test_shell_feedback_preserves_exit_timeout_and_truncation():
    result = _sandbox_tool_response('r1', 'execute_shell_command',
        {'exit_code': 7, 'timed_out': True, 'stdout': 'some output', 'stderr': 'ValueError: actual failure'})
    value = json.loads(result.content[0].text)
    assert value['exit_code'] == 7
    assert value['timed_out'] is True
    assert value['stdout'] == 'some output'
    assert value['stderr'] == 'ValueError: actual failure'
    assert value['stdout_truncated'] is False
    assert result.state.value == 'error'


def test_native_final_is_not_poisoned_by_old_parse_failure():
    middleware = BankRuntimeGatewayMiddleware(None, native_analysis_enabled=True)
    middleware.document_reads.failures['parse:f1'] = 'DOCUMENT_READ_FAILED'
    middleware.unresolved_file_operations.add('parse:f1')
    middleware._read_guard_failed = True
    assert middleware._read_error() == ''
    middleware._check_file_completion()
    assert middleware._source_block('execute_shell_command', {'command': 'python scratch/run.py'}) == ''


@pytest.mark.asyncio
async def test_native_model_response_is_not_filtered_by_old_conversion_scope_judge():
    middleware = BankRuntimeGatewayMiddleware(None, native_analysis_enabled=True)
    middleware.conversion_coverage = SimpleNamespace(requires_scope=True)
    async def forbidden_judge(response):
        raise AssertionError('Old scope prose judge must not run in native mode')
    middleware._scoped_model_response = forbidden_judge
    response = object()
    async def model(**kwargs):
        return response
    assert await middleware.on_model_call(SimpleNamespace(), {'tools': [], 'messages': []}, model) is response


def test_history_removes_native_container_capabilities_but_preserves_user_prose():
    raw = {'context': [
        {'role': 'user', 'content': [{'type': 'text', 'text': '保留用户原始 /workspace/input/example.xlsx'}]},
        {'role': 'assistant', 'content': [{'type': 'text', 'text': '普通回答原文保留。'}]},
        {'role': 'tool', 'content': [{'type': 'tool_result', 'name': 'runtime_sandbox_files_select',
            'state': 'success', 'output': [{'type': 'text', 'text': json.dumps({'selected_files': [
                {'file_id': 'f1', 'display_name': '原件.xlsx', 'container_path': '/workspace/input/f1/' + 'a'*64 + '.xlsx'}]})}]}]}
    ]}
    stored = _sanitize_agent_state(raw)
    assert stored['context'][0]['content'] == raw['context'][0]['content']
    assert stored['context'][1]['content'] == raw['context'][1]['content']
    assert '/workspace/input/f1/' not in str(stored['context'][2])
    assert '原件.xlsx' in str(stored['context'][2])
    assert 'f1' in str(stored['context'][2])


def test_history_preserves_business_json_keys_and_ordinary_assistant_paths():
    business = {'token': '业务字段', 'cursor': 12, 'container_path': '业务字段原值', 'content_hash': 'a'*64}
    raw = {'context': [
        {'role': 'user', 'content': [{'type': 'json', 'value': business}]},
        {'role': 'assistant', 'content': [{'type': 'text', 'text': '示例路径 /workspace/scratch/example.py，普通分析原文。'},
                                        {'type': 'json', 'value': business}]},
        {'role': 'tool', 'content': [{'type': 'tool_result', 'output': [{'type': 'text', 'text': json.dumps({
            'file_id': 'f1', 'content_hash': 'b'*64, 'container_path': '/workspace/input/f1/' + 'b'*64 + '.txt'})}]}]}
    ]}
    stored = _sanitize_agent_state(raw)
    assert stored['context'][:2] == raw['context'][:2]
    assert 'container_path' not in str(stored['context'][2])
    assert 'b'*64 in str(stored['context'][2])


def test_history_preserves_explanatory_attachment_xml_and_removes_marked_injection():
    example = '<runtime_attachment trusted="false">业务说明</runtime_attachment>'
    raw = {'context': [
        {'role': 'user', 'content': [{'type': 'text', 'text': example},
            {'type': 'text', 'id': 'trusted-injected-block', 'text': example}],
            'metadata': {'runtime_attachment_block_ids': ['trusted-injected-block']}},
        {'role': 'assistant', 'content': [{'type': 'text', 'text': '协议说明：' + example}]},
        {'role': 'tool', 'content': [{'type': 'tool_result', 'output': [{'type': 'text', 'text': example}]}]}
    ]}
    stored = _sanitize_agent_state(raw)
    assert stored['context'][0]['content'] == raw['context'][0]['content'][:1]
    assert stored['context'][1] == raw['context'][1]
    assert stored['context'][2]['content'][0]['output'] == []
    assert 'runtime_attachment_block_ids' not in str(stored)


@pytest.mark.parametrize('role', ['user', 'assistant'])
@pytest.mark.parametrize('prefix,suffix', [('', '\n请解释上面XML协议示例，保留我的问题。'),
                                         ('解释性前文：\n', ''), ('解释性前文：\n', '\n解释性后文。')])
def test_history_keeps_prose_around_xml_with_real_reference(role, prefix, suffix):
    reference = 'fr1_' + 'a'*64 + '_' + 'b'*64
    wrapper = '<runtime_attachment file_ref="' + reference + '">示例内容</runtime_attachment>'
    original = prefix + wrapper + suffix
    raw = {'context': [{'role': role, 'content': [{'type': 'text', 'text': original}]}]}
    stored = _sanitize_agent_state(raw)
    blocks = stored['context'][0]['content']
    assert len(blocks) == 1
    assert blocks[0]['text'] == (original if role == 'user' else original.replace(reference, '历史任务引用已移除'))


@pytest.mark.parametrize('size,digest,expected', [(12, 'a'*64, True), (False, 'a'*64, False),
    (12, 'sha256:' + 'a'*64, True), (-1, 'a'*64, False), (12, 'sha256:not-a-digest', False)])
def test_history_keeps_only_strict_stable_integrity_fields(size, digest, expected):
    from bank_runtime.sandbox.history import historical_file_metadata, selected_file_metadata
    item = {'file_id': 'f1', 'display_name': '原件.txt', 'content_type': 'text/plain',
            'size_bytes': size, 'content_hash': digest, 'container_path': '/workspace/input/private'}
    block = {'type': 'tool_result', 'name': 'runtime_sandbox_files_select', 'state': 'success',
             'output': [{'type': 'text', 'text': json.dumps({'selected_files': [item]})}]}
    selected = selected_file_metadata(block)[0]
    uploaded = historical_file_metadata(SimpleNamespace(state_dict=lambda: {'context': [
        {'role': 'user', 'metadata': {'runtime_attachment_metadata': [item]}}]}))['f1']
    assert uploaded == selected
    if expected:
        assert selected['size_bytes'] == 12
        assert selected['content_hash'] == 'a'*64
    else:
        assert ('size_bytes' in selected) is (type(size) is int and size >= 0)
        assert ('content_hash' in selected) is (digest == 'a'*64)
    assert 'container_path' not in selected


@pytest.mark.asyncio
async def test_native_shim_has_no_host_fallback_and_original_guard_denies(monkeypatch):
    from bank_runtime.sandbox.native_tools import native_function_tools
    from qwenpaw.security.tool_guard.engine import get_guard_engine
    engine = get_guard_engine()
    monkeypatch.setattr(engine, 'enabled', True)
    monkeypatch.setattr(engine, 'is_denied', lambda name: name == 'execute_shell_command')
    calls = []
    def guard(name, arguments, **kwargs):
        calls.append((name, arguments))
        return None
    monkeypatch.setattr(engine, 'guard', guard)
    from qwenpaw.security.tool_guard.virtual_paths import ContainerGuardPaths
    tools = native_function_tools(agent_id='trusted-agent', approval_level='smart', request_context={},
        guard_paths=ContainerGuardPaths(task_id='task'), active_scope=lambda:True)
    tool = next(tool for tool in tools if tool.name == 'execute_shell_command')
    assert tool.__class__.__name__ == 'GuardedFunctionTool'
    denied = await tool.check_permissions({'command': 'echo harmless'})
    assert denied.behavior == PermissionBehavior.DENY
    assert calls == [('execute_shell_command', {'command': 'echo harmless'})]
    with pytest.raises(RuntimeError):
        await tool(command='echo harmless')
    monkeypatch.setattr(tool, '_resolve_execution_level', lambda: 'off')
    denied = await tool.check_permissions({'command': 'echo harmless'})
    assert denied.behavior == PermissionBehavior.DENY


@pytest.mark.asyncio
async def test_gateway_runs_original_native_guard_after_preflight_and_audits_denial(monkeypatch):
    from bank_runtime.sandbox.native_tools import native_function_tools
    from bank_runtime.gateway.middleware import GatewayPermissionEngine
    from qwenpaw.security.tool_guard.engine import get_guard_engine
    events = []
    engine = get_guard_engine()
    monkeypatch.setattr(engine, 'enabled', True)
    monkeypatch.setattr(engine, 'is_denied', lambda _: True)
    monkeypatch.setattr(engine, 'guard', lambda name, arguments, **kwargs: events.append('original_guard'))
    class Client:
        async def preflight(self, *args, **kwargs):
            events.append('preflight')
            return {'tool_call_id': 'c1'}
        async def report_guard(self, *args, **kwargs):
            events.append(('audit', args[1]))
    class Delegate:
        async def check_permission(self, tool, arguments):
            return await tool.check_permissions(arguments)
    middleware = BankRuntimeGatewayMiddleware(Client(), native_analysis_enabled=True)
    gateway = GatewayPermissionEngine(Delegate(), middleware)
    from qwenpaw.security.tool_guard.virtual_paths import ContainerGuardPaths
    tool = native_function_tools(agent_id='a1', approval_level='smart', request_context={},
        guard_paths=ContainerGuardPaths(task_id='task'), active_scope=lambda:True)[0]
    denied = await gateway.check_permission(tool, {'command': 'python scratch/run.py'})
    assert denied.behavior == PermissionBehavior.DENY
    assert events == ['preflight', 'original_guard', ('audit', 'block')]
    assert not middleware._prepared


@pytest.mark.asyncio
async def test_native_preparation_exposes_only_original_metadata_and_keeps_media_blocks(tmp_path, monkeypatch):
    from bank_runtime.sandbox.cache import PreparedSandboxFile
    from bank_runtime.sandbox.scope import SandboxRequestScope
    from bank_runtime.sandbox.processor import AttachmentProcessor
    from bank_runtime.sandbox.tools import _prepared_blocks, SandboxToolState
    import hashlib
    content = b'sensitive ordinary original body'
    from bank_runtime.sandbox.file_refs import FileRefRegistry
    import bank_runtime.sandbox.tools as sandbox_tools
    registry = FileRefRegistry(root=tmp_path)
    monkeypatch.setattr(sandbox_tools, 'get_file_ref_registry', lambda: registry)
    (tmp_path / 't1').mkdir()
    path = tmp_path / 't1' / 'f1.txt'
    path.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    original = PreparedSandboxFile(file_id='f1', local_path=path, content_type='text/plain',
        size_bytes=len(content), original_name='原件.txt', expires_at='2099-01-01T00:00:00Z', task_id='t1', sha256=digest)
    scope = SandboxRequestScope('t1', {'native_analysis_enabled': True, 'isolation_level': 'container',
        'expires_at': '2099-01-01T00:00:00Z'}, (), ('f1',))
    metadata = {'file_id': 'f1', 'display_name': '原件.txt', 'content_type': 'text/plain', 'size_bytes': len(content),
                'content_hash': digest, 'container_path': '/workspace/input/f1/' + digest + '.txt'}
    class Broker:
        async def prepare_originals(self, current, files):
            assert current is scope
            assert files == [original]
            return [metadata]
    state = SandboxToolState(scope, Broker(), None, AttachmentProcessor())
    blocks = await _prepared_blocks(state, [original])
    text = blocks[0].text
    assert 'sensitive ordinary original body' not in text
    assert str(tmp_path) not in text
    assert 'container_path' in text
    assert scope.prepared_originals['f1'] == metadata


@pytest.mark.asyncio
async def test_native_install_requires_matching_projection_physical_context_and_guard_profile(monkeypatch):
    from bank_runtime.sandbox.hooks import BankRuntimeSandboxInstallHook, BankRuntimeSandboxCleanupHook
    from bank_runtime.sandbox.native_tools import NATIVE_TOOL_NAMES
    from bank_runtime.gateway.middleware import GatewayPermissionEngine
    import qwenpaw.config.config as config
    monkeypatch.setenv('QWENPAW_SERVICE_TOKEN', 'service-secret')
    monkeypatch.setattr(config, 'load_agent_config', lambda _: SimpleNamespace(approval_level='smart'))
    envelope = {'task_id': 't1', 'context_manifest_id': 'c1', 'signature': 'signed',
                'native_analysis_enabled': True, 'isolation_level': 'container', 'expires_at': '2099-01-01T00:00:00Z'}
    def context(snapshot='a' * 64, bound_agent_id='a1'):
        middleware = BankRuntimeGatewayMiddleware(SimpleNamespace(config=SimpleNamespace(agent_id=bound_agent_id)),
            sandbox_executor=SimpleNamespace(sandbox_context=dict(envelope)), native_analysis_enabled=True)
        request = SimpleNamespace(channel='bank-runtime', runtime_task_id='t1', user_id='u1',
            sandbox_context=dict(envelope), attachments_manifest=[],
            runtime_tool_gateway={'base_url': 'http://runtime', 'capability_snapshot_hash': 'a'*64},
            runtime_tool_visibility={'worker_type': 'qwenpaw', 'binding_snapshot_hash': 'sha256:' + snapshot,
                                     'authoritative': True, 'worker_tool_names': list(NATIVE_TOOL_NAMES)})
        agent = SimpleNamespace(toolkit=SimpleNamespace(tool_groups=[
            SimpleNamespace(tools=[SimpleNamespace(name='read_file', host_body=True)]),
            SimpleNamespace(tools=[SimpleNamespace(name='execute_shell_command', host_body=True)])]),
            _acting_middlewares=[middleware], _engine=GatewayPermissionEngine(SimpleNamespace(), middleware), _system_prompt='')
        return SimpleNamespace(request=request, agent=agent, agent_id='a1', extras={})
    ctx = context()
    try:
        await BankRuntimeSandboxInstallHook().run(ctx)
        installed = ctx.agent.toolkit.tool_groups[0].tools
        assert {tool.name for tool in installed} == NATIVE_TOOL_NAMES
        assert all(tool.__class__.__name__ == 'GuardedFunctionTool' for tool in installed)
        assert not ctx.agent.toolkit.tool_groups[1].tools
        assert 'header_row' not in ctx.agent._system_prompt
        assert 'numeric_text' not in ctx.agent._system_prompt
        assert 'temporary analysis files' in ctx.agent._system_prompt
        assert 'document-worker' in ctx.agent._system_prompt
        assert 'put deliverable files in /workspace/output' not in ctx.agent._system_prompt
    finally:
        await BankRuntimeSandboxCleanupHook().run(ctx)
    mismatch = context(snapshot='b'*64)
    try:
        await BankRuntimeSandboxInstallHook().run(mismatch)
        assert not mismatch.agent.toolkit.tool_groups[0].tools
        assert not mismatch.agent.toolkit.tool_groups[1].tools
    finally:
        await BankRuntimeSandboxCleanupHook().run(mismatch)
    foreign = context(bound_agent_id='other-agent')
    with pytest.raises(RuntimeError):
        await BankRuntimeSandboxInstallHook().run(foreign)
    assert all(not group.tools for group in foreign.agent.toolkit.tool_groups)
    monkeypatch.setattr(config, 'load_agent_config', lambda _: SimpleNamespace(approval_level='off'))
    disabled = context()
    with pytest.raises(RuntimeError):
        await BankRuntimeSandboxInstallHook().run(disabled)
    assert all(not group.tools for group in disabled.agent.toolkit.tool_groups)
