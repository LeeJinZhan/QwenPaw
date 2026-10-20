"""Cross-task history and auxiliary conversion regressions from real follow-ups."""
import copy
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bank_runtime.session import _sanitize_agent_state, _sanitize_value
from bank_runtime.gateway.middleware import BankRuntimeGatewayMiddleware
from bank_runtime.gateway.native_analysis import model_tool_names
from bank_runtime.sandbox.scope import SandboxRequestScope
from bank_runtime.sandbox.tools import SandboxToolState, set_sandbox_tool_state, reset_sandbox_tool_state
from test_conversion_coverage import Client, invoke
from bank_runtime.artifact_tools import ArtifactDeliveryIntent


@pytest.mark.parametrize('name', ['execute_shell_command', 'read_file', 'write_file',
    'edit_file', 'append_file', 'glob_search', 'grep_search', 'recall_history', 'recall_history_python'])
def test_persisted_history_drops_both_native_blocks_without_touching_prose_or_other_pairs(name):
    messages = [
        {'role': 'user', 'content': [{'type': 'text', 'text': '统计原件.xlsx'}],
         'metadata': {'runtime_attachment_metadata': [{'file_id': 'f1', 'display_name': '原件.xlsx'}]}},
        {'role': 'assistant', 'content': [{'type': 'tool_call', 'id': 'native', 'name': name,
            'input': json.dumps({'command': "p = '/workspace/input/old.xlsx'"})},
            {'type': 'thinking', 'text': 'old execution plan'},
            {'type': 'tool_call', 'id': 'other', 'name': 'knowledge_search', 'input': '{}'}]},
        {'role': 'tool', 'content': [{'type': 'tool_result', 'id': 'native',
            'output': [{'type': 'text', 'text': 'old script and private stdout'}]},
            {'type': 'tool_result', 'id': 'other', 'output': 'knowledge result'}]},
        {'role': 'assistant', 'content': [{'type': 'text', 'text': '上轮答复；示例 /workspace/input/example.xlsx'}]},
    ]
    raw = {'state': {'context': messages}, 'scroll': {'messages': copy.deepcopy(messages)}}
    before = copy.deepcopy(raw)
    clean = _sanitize_agent_state(raw)
    assert raw == before
    for projected in (clean['state']['context'], clean['scroll']['messages']):
        rendered = json.dumps(projected, ensure_ascii=False)
        assert 'native' not in rendered and 'private stdout' not in rendered
        assert 'old execution plan' not in rendered
        assert rendered.count('other') == 2
        assert projected[0] == messages[0]
        assert projected[-1] == messages[-1]


def test_historical_private_planning_is_removed_but_final_and_user_content_are_unchanged():
    final = {'type': 'text', 'text': '最终答复原文。'}
    user = {'role': 'user', 'content': [{'type': 'text', 'text': '用户问题原文。'}]}
    raw = {'state': {'context': [user,
        {'role': 'assistant', 'content': [{'type': 'thinking', 'text': 'old planning'}]},
        {'role': 'assistant', 'content': [{'type': 'reasoning', 'text': 'old planning'}, final]}]}}
    clean = _sanitize_agent_state(raw)
    assert clean['state']['context'] == [user, {'role': 'assistant', 'content': [final]}]


@pytest.mark.parametrize('split_messages', [False, True])
def test_native_history_keeps_final_blocks_without_replaying_tool_preambles(split_messages):
    # Real SDK storage can combine the whole ReAct turn into one AssistantMsg.
    # Removing tool pairs must not make its intermediate TextBlocks look final.
    blocks = [
        {'type': 'text', 'text': '先读取文件。'},
        {'type': 'tool_call', 'id': 'n1', 'name': 'execute_shell_command', 'input': '{}'},
        {'type': 'tool_result', 'id': 'n1', 'name': 'execute_shell_command', 'output': 'data'},
        {'type': 'text', 'text': '调整脚本后继续。'},
        {'type': 'tool_call', 'id': 'n2', 'name': 'read_file', 'input': '{}'},
        {'type': 'tool_result', 'id': 'n2', 'name': 'read_file', 'output': 'data'},
        {'type': 'text', 'text': '第一部分：统计结果。'},
        {'type': 'text', 'text': '第二部分：实际未读范围。'},
    ]
    user = {'role': 'user', 'content': [{'type': 'text', 'text': '原始问题，保留全文。'}]}
    assistants = ([{'role': 'assistant', 'content': [b]} for b in blocks] if split_messages
                  else [{'role': 'assistant', 'content': blocks}])
    raw = {'state': {'context': [user, *assistants]}}
    before = copy.deepcopy(raw)
    clean = _sanitize_agent_state(raw)
    assert raw == before
    projected = clean['state']['context']
    assert projected[0] == user
    assert [b['text'] for m in projected[1:] for b in m['content']] == [
        '第一部分：统计结果。', '第二部分：实际未读范围。']


def test_native_history_projection_does_not_guess_from_prose_or_cross_user_turns():
    plain = {'role': 'assistant', 'content': [
        {'type': 'text', 'text': '让我解释处理过程。'},
        {'type': 'text', 'text': '示例：/workspace/output/example.json'}]}
    raw = {'state': {'context': [
        {'role': 'user', 'content': [{'type': 'text', 'text': '解释一个概念'}]}, plain,
        {'role': 'user', 'content': [{'type': 'text', 'text': '执行当前任务'}]},
        {'role': 'assistant', 'content': [
            {'type': 'text', 'text': '当前执行说明'},
            {'type': 'tool_call', 'id': 'n', 'name': 'execute_shell_command', 'input': '{}'},
            {'type': 'tool_result', 'id': 'n', 'name': 'execute_shell_command', 'output': 'ok'},
            {'type': 'text', 'text': '当前结果'}]}]}}
    clean = _sanitize_agent_state(raw)
    assert clean['state']['context'][1] == plain
    assert clean['state']['context'][-1]['content'] == [{'type': 'text', 'text': '当前结果'}]


@pytest.mark.asyncio
async def test_sdk_formatted_followup_has_final_history_without_execution_narrative():
    from agentscope.message import Msg, AssistantMsg, UserMsg, TextBlock, ToolCallBlock, ToolResultBlock, ToolResultState
    from agentscope.formatter import OpenAIChatFormatter
    from bank_runtime.model_context import prepare_public_model_context
    history = [UserMsg('user', '分析原件'), AssistantMsg('assistant', [
        TextBlock(text='先读取原件，再修正脚本。'),
        ToolCallBlock(id='native', name='execute_shell_command', input='{}'),
        ToolResultBlock(id='native', name='execute_shell_command', state=ToolResultState.SUCCESS, output='ok'),
        TextBlock(text='最终分析结果：当前仅确认两个工作表。'),
        TextBlock(text='其余工作表尚未读取。')]), UserMsg('user', '按工作表重新组织结果')]
    formatter = OpenAIChatFormatter()
    before = await formatter.format(history)
    saved = _sanitize_agent_state({'state': {'context': [m.model_dump(mode='json') for m in history]}})
    restored = [Msg.model_validate(m) for m in saved['state']['context']]
    after = await formatter.format(prepare_public_model_context({'messages':restored,'tools':[]})['messages'])
    assert '先读取原件，再修正脚本。' in str(before)
    assert '先读取原件，再修正脚本。' not in str(after)
    assert '最终分析结果：当前仅确认两个工作表。' in str(after)
    assert '其余工作表尚未读取。' in str(after)
    assert '内部临时数据' in str(after)
    assert '最终正文只给当前要求的结果' in str(after)
    assert history[1].content[0].text == '先读取原件，再修正脚本。'


@pytest.mark.asyncio
@pytest.mark.parametrize('native', [True, False])
async def test_managed_native_history_cannot_reenter_unprojected_scroll_archive(tmp_path, native):
    from test_managed_session import _ctx, _request, _prepare_and_load
    from bank_runtime.session import ManagedSessionCleanupHook
    from qwenpaw.app.chats.session import SafeJSONSession
    from qwenpaw.runtime.builder import AgentBuilder
    request = _request()
    request.sandbox_context = {'native_analysis_enabled': native, 'isolation_level': 'container'}
    ctx = _ctx(SafeJSONSession(str(tmp_path)), request=request)
    try:
        await _prepare_and_load(ctx)
        config = SimpleNamespace(running=SimpleNamespace(light_context_config=SimpleNamespace(
            strategy='scroll', context_compact_config=SimpleNamespace(enabled=True))))
        actual = AgentBuilder._apply_context_history_policy(config, ctx)
        assert actual.running.light_context_config.strategy == ('native' if native else 'scroll')
        assert config.running.light_context_config.strategy == 'scroll'
        assert actual.running.light_context_config.context_compact_config.enabled is True
    finally:
        await ManagedSessionCleanupHook().run(ctx)


@pytest.mark.parametrize('quote', ["'", '"'])
def test_legacy_path_redaction_preserves_python_string_delimiters(quote):
    original = f'p = {quote}/workspace/input/old.xlsx{quote}\nprint(p)'
    compile(_sanitize_value(original, tool_payload=True), 'history', 'exec')


@pytest.mark.asyncio
async def test_old_session_load_and_new_commit_project_native_pairs_without_mutating_active_agent(tmp_path):
    from test_managed_session import _ctx, _request, _Agent, _prepare_and_load, _commit_and_cleanup
    from bank_runtime.session import ManagedSessionCleanupHook
    from qwenpaw.app.chats.session import SafeJSONSession
    messages = [
        {'role': 'user', 'content': [{'type': 'text', 'text': '继续读取原件'}]},
        {'role': 'assistant', 'content': [{'type': 'tool_call', 'id': 'old-native',
            'name': 'execute_shell_command', 'input': '{"command":"old temporary script"}'}]},
        {'role': 'tool', 'content': [{'type': 'tool_result', 'id': 'old-native', 'output': 'old output'}]},
        {'role': 'assistant', 'content': [{'type': 'text', 'text': '上轮结论'}]},
    ]
    original = {'state': {'context': messages}}
    delegate = SafeJSONSession(str(tmp_path))
    ctx = _ctx(delegate, agent=_Agent(original))
    await _prepare_and_load(ctx)
    await _commit_and_cleanup(ctx)
    saved = await delegate.get_session_state_dict(session_id='session-001', user_id='user-a', channel='bank-runtime')
    assert 'old-native' not in json.dumps(saved['agent'])
    assert ctx.agent.state == original
    legacy = copy.deepcopy(saved)
    legacy['agent'] = original
    class LegacySession:
        async def get_session_state_dict(self, **kwargs):
            return legacy
    resumed = _ctx(LegacySession(), request=_request(task_id='next-task', session_state='active'))
    try:
        await _prepare_and_load(resumed)
        assert resumed.session_state['state']['context'] == [messages[0], messages[-1]]
        assert legacy['agent'] == original
    finally:
        await ManagedSessionCleanupHook().run(resumed)


@pytest.fixture
def native_scope():
    context = {'native_analysis_enabled': True, 'isolation_level': 'container',
               'analysis_environment': {'packages': {'openpyxl': '3.1.5', 'xlrd': '2.0.2'}}}
    scope = SandboxRequestScope('task', context, (), ())
    scope.historical_files['f1'] = {'file_id': 'f1', 'display_name': '原件.xlsx'}
    token = set_sandbox_tool_state(SandboxToolState(scope, None, None, None))
    try:
        yield scope
    finally:
        reset_sandbox_tool_state(token)


def test_unprepared_historical_tables_route_to_selection_without_granting_file_access(native_scope):
    allowed = {'execute_shell_command', 'MinerU__parse_documents', 'runtime_sandbox_files_search',
               'runtime_sandbox_files_select', 'artifact_convert', 'artifact_generate'}
    actual = model_tool_names(allowed, SimpleNamespace(sandbox_context=native_scope.sandbox_context))
    assert actual == allowed - {'MinerU__parse_documents'}
    assert not native_scope.prepared_originals and not native_scope.selected_file_ids
    native_scope.historical_files['pdf'] = {'file_id': 'pdf', 'display_name': '扫描件.pdf'}
    assert model_tool_names(allowed, SimpleNamespace(sandbox_context=native_scope.sandbox_context)) == allowed


@pytest.mark.parametrize('missing_reader,no_shell', [(True, False), (False, True)])
def test_historical_hint_does_not_remove_extractor_when_native_reader_is_unavailable(native_scope, missing_reader, no_shell):
    allowed = {'MinerU__parse_documents'} | (set() if no_shell else {'execute_shell_command'})
    if missing_reader:
        native_scope.sandbox_context['analysis_environment']['packages'] = {'openpyxl': None}
    assert model_tool_names(allowed, SimpleNamespace(sandbox_context=native_scope.sandbox_context)) == allowed


def test_current_file_context_reports_selection_state_without_reusing_old_access_claims(native_scope):
    from bank_runtime.gateway.native_analysis import model_file_context
    executor = SimpleNamespace(sandbox_context=native_scope.sandbox_context)
    native_scope.historical_files['f1']['token'] = 'must-not-appear'
    before = model_file_context({'runtime_sandbox_files_select'}, executor)
    assert '"task_id": "task"' in before
    assert '"file_id": "f1"' in before
    assert '"preparation": "not_prepared"' in before
    assert 'must-not-appear' not in before
    native_scope.selected_file_ids.add('f1')
    native_scope.prepared_originals['f1'] = {'container_path': '/workspace/input/f1/a.xlsx',
                                           'token': 'another-secret'}
    after = model_file_context({'runtime_sandbox_files_select'}, executor)
    assert '"preparation": "prepared"' in after
    assert '/workspace/input/f1/a.xlsx' in after
    assert 'another-secret' not in after
    assert 'not_prepared' in before  # Earlier snapshots are immutable.
    assert model_file_context(set(), executor) == ''
    assert model_file_context({'runtime_sandbox_files_select'}, None) == ''


@pytest.mark.asyncio
async def test_current_file_state_reaches_model_call_without_changing_history(native_scope):
    from agentscope.message import UserMsg, AssistantMsg
    history = [UserMsg('user', '检查原件'), AssistantMsg('assistant', '此前未能读取'),
               UserMsg('user', '换个范围')]
    before = copy.deepcopy(history)
    middleware = BankRuntimeGatewayMiddleware(None, native_analysis_enabled=True,
        sandbox_executor=SimpleNamespace(sandbox_context=native_scope.sandbox_context))
    middleware.allowed_tool_names = frozenset({'runtime_sandbox_files_select'})
    async def capture(**kwargs):
        return kwargs
    request = {'messages': history, 'tools': [{'type': 'function',
               'function': {'name': 'runtime_sandbox_files_select'}}]}
    result = await middleware.on_model_call(None, request, capture)
    reminder = result['messages'][-1].get_text_content()
    assert '"preparation": "not_prepared"' in reminder
    assert '"file_id": "f1"' in reminder
    assert '当前任务' in reminder
    assert history == before


@pytest.mark.asyncio
async def test_native_model_context_projects_old_file_control_results_but_keeps_current_and_delivery(native_scope):
    from agentscope.message import UserMsg, AssistantMsg, ToolCallBlock, ToolResultBlock, ToolResultState
    from agentscope.formatter import OpenAIChatFormatter
    old = [
        UserMsg('user', '先前的问题'),
        AssistantMsg('assistant', [ToolCallBlock(id='old-select', name='runtime_sandbox_files_select', input='{}'),
            ToolCallBlock(id='old-convert', name='artifact_convert', input='{"purpose":"read"}'),
            ToolCallBlock(id='delivery', name='artifact_convert', input='{"purpose":"delivery"}')]),
        AssistantMsg('assistant', [ToolResultBlock(id='old-select', name='runtime_sandbox_files_select',
            state=ToolResultState.DENIED, output='old permission failure'),
            ToolResultBlock(id='old-convert', name='artifact_convert', state=ToolResultState.ERROR, output='old conversion failure'),
            ToolResultBlock(id='delivery', name='artifact_convert', state=ToolResultState.SUCCESS, output='published file')]),
        AssistantMsg('assistant', '历史正文保留'),
        UserMsg('user', '继续检查'),
        AssistantMsg('assistant', [ToolCallBlock(id='current', name='runtime_sandbox_files_select', input='{}'),
            ToolResultBlock(id='current', name='runtime_sandbox_files_select', state=ToolResultState.DENIED, output='current denial')]),
    ]
    before = copy.deepcopy(old)
    middleware = BankRuntimeGatewayMiddleware(None, native_analysis_enabled=True,
        sandbox_executor=SimpleNamespace(sandbox_context=native_scope.sandbox_context))
    async def capture(**kwargs):
        return kwargs
    result = await middleware.on_model_call(None, {'messages': old, 'tools': []}, capture)
    wire = await OpenAIChatFormatter().format(result['messages'])
    rendered = str(wire)
    assert 'old-select' not in rendered and 'old-convert' not in rendered
    assert 'old permission failure' not in rendered and 'old conversion failure' not in rendered
    assert 'delivery' in rendered and 'published file' in rendered
    assert 'current' in rendered and 'current denial' in rendered
    assert '历史正文保留' in rendered
    assert old == before


def test_auxiliary_argument_failure_keeps_recovery_restriction_but_not_delivery_failure():
    middleware = BankRuntimeGatewayMiddleware(None, native_analysis_enabled=True)
    payload = {'source_id': 'f1', 'purpose': 'read', 'target_format': 'xlsx'}
    for _ in range(2):
        middleware._record_artifact_input_failure('artifact_convert', payload)
    assert middleware.artifact_input_failures == 0
    assert middleware.artifact_recovery.blocked_reason('artifact_convert', payload)
    middleware._check_file_completion()
    middleware._record_artifact_input_failure('artifact_generate', {'artifact_type': 'xlsx'})
    assert middleware.artifact_input_failures == 1
    with pytest.raises(Exception) as caught:
        middleware._check_file_completion()
    assert caught.value.error_code == 'ARTIFACT_VALIDATION_FAILED'


def test_auxiliary_conversion_diagnostic_does_not_replace_unrelated_required_delivery_error():
    middleware = BankRuntimeGatewayMiddleware(None, native_analysis_enabled=True)
    auxiliary = {'source_id': 'f1', 'purpose': 'read', 'target_format': 'xlsx'}
    middleware.unresolved_file_operations.update(middleware._operation_keys('artifact_convert', auxiliary))
    middleware._remember_conversion_failure('artifact_convert', auxiliary, 'office_conversion_failed')
    middleware.unresolved_file_operations.update(middleware._operation_keys('artifact_generate', {'artifact_type': 'docx'}))
    with pytest.raises(Exception) as caught:
        middleware._check_file_completion()
    assert caught.value.error_code == 'ARTIFACT_OUTPUT_MISSING'


@pytest.mark.asyncio
@pytest.mark.parametrize('native,purpose,required', [
    (True, 'read', False), (True, 'delivery', False), (False, 'read', False), (True, 'read', True)])
async def test_failed_auxiliary_conversion_is_not_a_required_deliverable(native, purpose, required):
    client = Client()
    client.envelope = {'status': 'failed', 'error_code': 'ARTIFACT_RENDER_FAILED', 'result': {}}
    middleware = BankRuntimeGatewayMiddleware(client, native_analysis_enabled=native,
        artifact_intent=ArtifactDeliveryIntent(operation='convert', target_format='xlsx') if required else None)
    await invoke(middleware, 'artifact_convert', {'source_type': 'session_file', 'source_id': 'f1',
        'target_format': 'xlsx', 'purpose': purpose})
    if native and purpose == 'read' and not required:
        middleware._check_file_completion()
        assert middleware.unresolved_file_operations  # Failure is retained, never relabeled success.
    else:
        with pytest.raises(Exception) as caught:
            middleware._check_file_completion()
        assert getattr(caught.value, 'error_code', '') == 'ARTIFACT_OUTPUT_MISSING'


@pytest.mark.asyncio
@pytest.mark.parametrize('suffix,purpose,worker_called', [
    ('.xlsx', 'read', False), ('.xlsm', 'read', False), ('.xls', 'read', True),
    ('.xlsx', 'delivery', True), ('.pdf', 'read', True)])
async def test_only_native_readable_same_format_internal_copy_is_stopped_after_guard(native_scope, suffix, purpose, worker_called, monkeypatch):
    async def no_attachment_blocks(*args):
        return []
    monkeypatch.setattr('bank_runtime.sandbox.tools.converted_attachment_blocks', no_attachment_blocks)
    native_scope.prepared_originals['f1'] = {'container_path': '/workspace/input/f1/' + 'a'*64 + suffix}
    native_scope.selected_file_ids.add('f1')
    client = Client()
    middleware = BankRuntimeGatewayMiddleware(client, native_analysis_enabled=True,
        sandbox_executor=SimpleNamespace(sandbox_context=native_scope.sandbox_context))
    result = await invoke(middleware, 'artifact_convert', {'source_type': 'session_file', 'source_id': 'f1',
        'target_format': 'xlsx' if suffix == '.xls' else suffix[1:], 'purpose': purpose})
    assert bool(client.executions) is worker_called
    if not worker_called:
        assert client.reports
        assert result[0].state.value == 'error'
        assert 'NATIVE_ORIGINAL_READ_AVAILABLE' in str(result[0].content)
        middleware._check_file_completion()
