"""Current-session public recall reaches the SDK without granting raw archive access."""
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from agentscope.message import SystemMsg, UserMsg, ToolCallBlock
from agentscope.state import AgentState
from agentscope.tool import Toolkit, FunctionTool
from agentscope.permission import PermissionBehavior
from qwenpaw.agents.context.scroll.manager import ScrollContextManager
from qwenpaw.agents.context.scroll.history import HistoryStore
from qwenpaw.app.chats.session import SafeJSONSession
from test_personalization import _request
from test_personal_skill_boundary import _Guard
from test_managed_session import _ctx, _prepare_and_load
from bank_runtime.session import ManagedSessionCleanupHook
from bank_runtime.personalization import BankRuntimePersonalizationHook, BankRuntimePersonalizationCleanupHook
from bank_runtime.gateway.middleware import BankRuntimeGatewayMiddleware, BankRuntimeGatewayInstallHook
from bank_runtime.gateway.hooks import BankRuntimeToolVisibilityHook
from bank_runtime.gateway.client import GatewayError


class _Client:
    config = SimpleNamespace(agent_id='assistant-a', task_id='task_001', session_id='session-001', base_url='https://runtime.internal')
    async def preflight(self, *_args, **_kwargs):
        raise AssertionError('Context recall cannot acquire a business permit')


async def build(tmp_path, monkeypatch):
    monkeypatch.setenv('QWENPAW_SERVICE_TOKEN', 'synthetic-service-token')
    monkeypatch.setattr('qwenpaw.config.config.load_agent_config', lambda _agent_id: SimpleNamespace(approval_level='auto'))
    history = HistoryStore(tmp_path / 'history.db')
    cm = ScrollContextManager(history=history, session_id='session-001', agent_id='assistant-a')
    request = _request(session_id='session-001', runtime_task_id='task_001', session_contract_version='2.0',
        qwenpaw_session_state='uninitialized', session_operation='append',
        sandbox_context={'task_id':'task_001', 'context_id':'ctx', 'signature':'signed', 'native_analysis_enabled':False},
        runtime_tool_gateway={'capability_snapshot_hash':'a'*64},
        runtime_tool_visibility={'authoritative':True, 'worker_type':'qwenpaw', 'binding_snapshot_hash':'sha256:'+'a'*64, 'worker_tool_names':[]})
    mw = BankRuntimeGatewayMiddleware(_Client())
    agent = SimpleNamespace(toolkit=Toolkit(), state=AgentState(), _system_prompt='BASE',
        _acting_middlewares=[mw], _engine=_Guard(), _context_manager=cm)
    ctx = _ctx(SafeJSONSession(str(tmp_path / 'sessions')), request=request, agent=agent)
    ctx.input_msgs = []
    await _prepare_and_load(ctx)
    await BankRuntimePersonalizationHook().run(ctx)
    await BankRuntimeGatewayInstallHook().run(ctx)
    await BankRuntimeToolVisibilityHook().run(ctx)
    return ctx, mw, history


async def cleanup(ctx, history):
    await BankRuntimePersonalizationCleanupHook().run(ctx)
    await ManagedSessionCleanupHook().run(ctx)
    history.close()


@pytest.mark.asyncio
async def test_controlled_recall_survives_visibility_and_uses_only_fixed_runtime_endpoint(tmp_path, monkeypatch):
    ctx, mw, history = await build(tmp_path, monkeypatch)
    calls = []
    try:
        async def post(path, body, **kwargs):
            calls.append((path, body))
            return {'messages':[{'role':'assistant', 'turn_id':'turn_old', 'content':[{'type':'text', 'text':'历史公开答复'}]}],
                    'has_more':False, 'next_before_turn_id':''}
        monkeypatch.setattr(mw.history_recall.broker, '_post', post)
        captured = {}
        async def model(**kw):
            captured.update(kw)
        await mw.on_model_call(ctx.agent, {'messages':[SystemMsg('system','BASE'),UserMsg('user','回忆季度结果')],
                                           'tools':await ctx.agent.toolkit.get_tool_schemas()}, model)
        assert [s['function']['name'] for s in captured['tools']] == ['recall_history']
        assert 'before_turn_id' in str(captured['messages'])
        tool = await ctx.agent.toolkit.check_tool_available('recall_history', [])
        payload = {'query':'季度'}
        assert (await ctx.agent._engine.check_permission(tool, payload)).behavior == PermissionBehavior.ALLOW
        call = ToolCallBlock(id='recall', name='recall_history', input=json.dumps(payload))
        async def execute():
            async for item in ctx.agent.toolkit.call_tool(call, ctx.agent.state):
                yield item
        output = [item async for item in mw.on_acting(ctx.agent, {'tool_call':call}, execute)]
        assert '历史公开答复' in str(output)
        assert calls[0][0] == '/runtime/internal/conversation/context/expand'
        assert calls[0][1]['sandbox_context'] == ctx.request.sandbox_context
        assert 'conversation_id' not in calls[0][1]
        with pytest.raises(GatewayError):
            _ = [item async for item in mw.on_acting(ctx.agent, {'tool_call':call}, execute)]
        assert len(calls) == 1
    finally:
        await cleanup(ctx, history)


@pytest.mark.asyncio
async def test_recall_rejects_scope_parameters_swapped_tools_guard_denial_and_cleanup(tmp_path, monkeypatch):
    ctx, mw, history = await build(tmp_path, monkeypatch)
    try:
        tool = mw.history_recall.tool
        for payload in ({'session_id':'other'}, {'all_agents':True}, {'path':'/etc/passwd'},
                        {'limit':True}, {'limit':33}, {'query':'x'*201}):
            assert (await ctx.agent._engine.check_permission(tool, payload)).behavior == PermissionBehavior.DENY
        delegate = ctx.agent._engine.delegate
        delegate.behavior = PermissionBehavior.DENY
        assert (await ctx.agent._engine.check_permission(tool, {})).behavior == PermissionBehavior.DENY
        delegate.behavior = PermissionBehavior.ALLOW
        assert (await ctx.agent._engine.check_permission(tool, {})).behavior == PermissionBehavior.ALLOW
        await ManagedSessionCleanupHook().run(ctx)
        assert not await mw.history_recall.visible()
        assert (await ctx.agent._engine.check_permission(tool, {})).behavior == PermissionBehavior.DENY
    finally:
        await cleanup(ctx, history)


@pytest.mark.asyncio
@pytest.mark.parametrize('model_name', ['deepseek-v4-flash', 'qwen3-27b'])
async def test_recall_reaches_sdk_with_one_leading_system_message(tmp_path, monkeypatch, model_name):
    from agentscope.credential import OpenAICredential
    from agentscope.formatter import OpenAIChatFormatter
    from openai.resources.chat.completions import AsyncCompletions
    from openai.types.chat import ChatCompletion
    from qwenpaw.providers.openai_chat_model_compat import OpenAIChatModelCompat
    ctx, mw, history = await build(tmp_path, monkeypatch)
    captured = {}
    async def create(_self, **kwargs):
        captured.update(kwargs)
        return ChatCompletion(id='synthetic', object='chat.completion', created=0,
            model=model_name, choices=[{'index':0, 'finish_reason':'stop',
                'message':{'role':'assistant', 'content':'Synthetic response'}}])
    monkeypatch.setattr(AsyncCompletions, 'create', create)
    try:
        model = OpenAIChatModelCompat(credential=OpenAICredential(api_key='synthetic', base_url='https://sdk-probe.invalid'),
            model=model_name, stream=False, max_retries=0, formatter=OpenAIChatFormatter())
        msgs = [SystemMsg('system','BASE'), UserMsg('user','历史原话')]
        await mw.on_model_call(ctx.agent, {'messages':msgs, 'tools':await ctx.agent.toolkit.get_tool_schemas()}, model)
        assert [s['function']['name'] for s in captured['tools']] == ['recall_history']
        assert [m['role'] for m in captured['messages']] == ['system', 'user']
        assert 'before_turn_id' in str(captured['messages'][0])
        assert len(msgs) == 2
    finally:
        await cleanup(ctx, history)


@pytest.mark.asyncio
async def test_recall_tool_substitution_and_scope_mutation_fail_closed(tmp_path, monkeypatch):
    ctx, mw, history = await build(tmp_path, monkeypatch)
    try:
        tool = mw.history_recall.tool
        async def forged():
            raise AssertionError('Must never execute')
        replacement = FunctionTool(forged, name='recall_history', is_read_only=True)
        for group in ctx.agent.toolkit.tool_groups:
            group.tools[:] = [replacement if item is tool else item for item in group.tools]
        assert not await mw.history_recall.visible()
        captured = {}
        async def model(**kwargs):
            captured.update(kwargs)
        await mw.on_model_call(ctx.agent, {'messages':[], 'tools':await ctx.agent.toolkit.get_tool_schemas()}, model)
        assert captured['tools'] == []
        assert not mw.history_recall.recognizes(replacement)
        for group in ctx.agent.toolkit.tool_groups:
            group.tools[:] = [tool if item is replacement else item for item in group.tools]
        assert await mw.history_recall.visible()
        original = tool._func
        tool._func = forged
        assert not await mw.history_recall.visible()
        tool._func = original
        ctx.request.sandbox_context = {**ctx.request.sandbox_context, 'task_id':'other-task'}
        assert not await mw.history_recall.visible()
        assert (await ctx.agent._engine.check_permission(tool, {})).behavior == PermissionBehavior.DENY
    finally:
        await cleanup(ctx, history)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['tool_role', 'media', 'oversize', 'bad_cursor', 'cleanup', 'denied'])
async def test_recall_never_returns_unsafe_or_stale_response(tmp_path, monkeypatch, kind):
    ctx, mw, history = await build(tmp_path, monkeypatch)
    async def post(*_args, **_kwargs):
        if kind == 'denied':
            raise RuntimeError('sensitive transport failure')
        if kind == 'cleanup':
            await ManagedSessionCleanupHook().run(ctx)
        return {'messages':[{'role':'tool' if kind == 'tool_role' else 'assistant', 'turn_id':'turn_old',
            'reasoning_content':'private reasoning',
            'content':[{'type':'image' if kind == 'media' else 'text', 'text':'x'*70000 if kind == 'oversize' else 'private sentinel'}]}],
            'has_more':False, 'next_before_turn_id':'x'*97 if kind == 'bad_cursor' else ''}
    try:
        monkeypatch.setattr(mw.history_recall.broker, '_post', post)
        result = await mw.history_recall.read()
        assert result.state.value == 'error'
        assert 'private sentinel' not in str(result)
        assert 'private reasoning' not in str(result)
        assert 'sensitive transport' not in str(result)
    finally:
        await cleanup(ctx, history)


@pytest.mark.asyncio
async def test_native_mode_does_not_register_scroll_reader(tmp_path, monkeypatch):
    ctx, mw, history = await build(tmp_path, monkeypatch)
    try:
        ctx.request.sandbox_context = {**ctx.request.sandbox_context, 'native_analysis_enabled':True, 'isolation_level':'container'}
        mw.bind_native_skills(ctx.agent, ctx.request)
        assert not mw.history_recall.eligible()
        assert 'recall_history' not in [s['function']['name'] for s in await ctx.agent.toolkit.get_tool_schemas()]
    finally:
        await cleanup(ctx, history)


@pytest.mark.asyncio
async def test_recall_refuses_another_request_and_guard_argument_mutation(tmp_path, monkeypatch):
    from bank_runtime.session import _current_scope
    ctx, mw, history = await build(tmp_path, monkeypatch)
    try:
        reader = mw.history_recall
        for user_id in ('user-a', 'other-user'):
            token = _current_scope.set(replace(reader.scope, user_id=user_id))
            try:
                assert not await reader.visible()
                assert (await ctx.agent._engine.check_permission(reader.tool, {})).behavior == PermissionBehavior.DENY
            finally:
                _current_scope.reset(token)
        delegate = ctx.agent._engine.delegate
        original = delegate.check_permission
        async def mutate(tool, payload):
            decision = await original(tool, payload)
            payload['query'] = 'altered'
            return decision
        monkeypatch.setattr(delegate, 'check_permission', mutate)
        assert (await ctx.agent._engine.check_permission(reader.tool, {})).behavior == PermissionBehavior.DENY
        assert not mw._prepared
    finally:
        await cleanup(ctx, history)


@pytest.mark.asyncio
@pytest.mark.parametrize('denied', [True, False])
async def test_recall_honors_real_qwen_tool_guard_denied_list(tmp_path, monkeypatch, denied):
    from unittest.mock import MagicMock
    from agentscope.permission import PermissionEngine, PermissionContext
    from qwenpaw.security.tool_guard.models import ToolGuardResult
    ctx, mw, history = await build(tmp_path, monkeypatch)
    engine = MagicMock()
    engine.enabled = True
    engine.is_denied.return_value = denied
    engine.guard.return_value = ToolGuardResult(tool_name='recall_history', params={})
    monkeypatch.setattr('qwenpaw.security.tool_guard.engine.get_guard_engine', lambda: engine)
    ctx.agent._engine.delegate = PermissionEngine(PermissionContext())
    try:
        decision = await ctx.agent._engine.check_permission(mw.history_recall.tool, {})
        assert decision.behavior == (PermissionBehavior.DENY if denied else PermissionBehavior.ALLOW)
        engine.guard.assert_called_once()
        assert bool(mw._prepared) is (not denied)
    finally:
        await cleanup(ctx, history)


@pytest.mark.asyncio
async def test_recall_disappears_when_guard_configuration_is_unavailable(tmp_path, monkeypatch):
    ctx, mw, history = await build(tmp_path, monkeypatch)
    def unavailable(_agent_id):
        raise RuntimeError('configuration unavailable')
    try:
        monkeypatch.setattr('qwenpaw.config.config.load_agent_config', unavailable)
        assert not await mw.history_recall.visible()
        assert (await ctx.agent._engine.check_permission(mw.history_recall.tool, {})).behavior == PermissionBehavior.DENY
    finally:
        await cleanup(ctx, history)
