"""Personal context retrieval survives business-tool filtering without new grants."""
import json
from types import SimpleNamespace

import pytest
from agentscope.formatter import OpenAIChatFormatter
from agentscope.message import SystemMsg, UserMsg, ToolCallBlock, ToolResultState
from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.state import AgentState
from agentscope.tool import FunctionTool, Toolkit

from test_personalization import _request, _skill_bytes, _skill_payloads
from bank_runtime.personal_skills import DownloadedPersonalSkill
from bank_runtime.personalization import (
    BankRuntimePersonalizationHook, BankRuntimePersonalizationCleanupHook,
    current_personalization_context,
)
from bank_runtime.gateway.hooks import BankRuntimeToolVisibilityHook
from bank_runtime.gateway.client import GatewayError
from bank_runtime.gateway.middleware import BankRuntimeGatewayInstallHook, BankRuntimeGatewayMiddleware


class _NoBusinessCalls:
    async def preflight(self, *_args, **_kwargs):
        raise AssertionError("Personal method retrieval is not a business action")


class _Guard:
    def __init__(self):
        self.behavior = PermissionBehavior.ALLOW
        self.calls = 0

    async def check_permission(self, _tool, _input):
        self.calls += 1
        return PermissionDecision(behavior=self.behavior, message='test guard')


async def _build(*, agent_id=None, **overrides):
    body = _skill_bytes()
    catalog, manifest = _skill_payloads(body)
    snapshot = 'a' * 64
    request = _request(
        personal_skills_catalog=catalog, personal_skills_access_manifest=manifest,
        runtime_tool_visibility={'worker_type': 'qwenpaw', 'worker_tool_names': [],
            'binding_snapshot_hash': 'sha256:' + snapshot, 'authoritative': True},
        runtime_tool_gateway={'capability_snapshot_hash': snapshot},
    )
    for key, value in overrides.items():
        setattr(request, key, value)
    guard = _Guard()
    middleware = BankRuntimeGatewayMiddleware(_NoBusinessCalls())
    agent = SimpleNamespace(_system_prompt='BASE', toolkit=Toolkit(),
        state=AgentState(),
        _acting_middlewares=[middleware], _engine=guard)
    if agent_id is not None:
        agent._agent_config = SimpleNamespace(id=agent_id)
    ctx = SimpleNamespace(request=request, agent=agent, extras={}, input_msgs=[])
    await BankRuntimePersonalizationHook().run(ctx)
    await BankRuntimeGatewayInstallHook().run(ctx)
    await BankRuntimeToolVisibilityHook().run(ctx)
    return ctx, middleware, guard, body


@pytest.mark.asyncio
async def test_personal_loader_reaches_final_model_request_without_business_grants(monkeypatch):
    ctx, middleware, _guard, _body = await _build()
    try:
        messages = [SystemMsg('system', ctx.agent._system_prompt), UserMsg('user', '整理月报')]
        wire = await OpenAIChatFormatter().format(messages)
        assert 'personal:skill_001' in str(wire)
        assert 'secret=token' not in str(wire)
        schemas = await ctx.agent.toolkit.get_tool_schemas()
        captured = {}
        async def model(**kwargs):
            captured.update(kwargs)
            return None
        await middleware.on_model_call(ctx.agent, {'messages': messages, 'tools': schemas}, model)
        assert [s['function']['name'] for s in captured['tools']] == ['activate_personal_skill']
        from agentscope.credential import OpenAICredential
        from openai.resources.chat.completions import AsyncCompletions
        from openai.types.chat import ChatCompletion
        from qwenpaw.providers.openai_chat_model_compat import OpenAIChatModelCompat
        sdk_request = {}
        async def create(_self, **kwargs):
            sdk_request.update(kwargs)
            return ChatCompletion(id='synthetic', object='chat.completion', created=0,
                model='synthetic', choices=[{'index': 0, 'finish_reason': 'stop',
                    'message': {'role': 'assistant', 'content': 'Synthetic response'}}])
        monkeypatch.setattr(AsyncCompletions, 'create', create)
        sdk_model = OpenAIChatModelCompat(
            credential=OpenAICredential(api_key='synthetic', base_url='https://sdk-probe.invalid'),
            model='synthetic', stream=False, max_retries=0, formatter=OpenAIChatFormatter())
        await middleware.on_model_call(ctx.agent, {'messages': messages, 'tools': schemas}, sdk_model)
        assert [s['function']['name'] for s in sdk_request['tools']] == ['activate_personal_skill']
        assert 'personal:skill_001' in str(sdk_request['messages'])
        assert 'secret=token' not in str(sdk_request)
    finally:
        await BankRuntimePersonalizationCleanupHook().run(ctx)


@pytest.mark.asyncio
@pytest.mark.parametrize('scheme', ['https', 'http'])
async def test_personal_loader_runs_through_guard_and_request_registry(monkeypatch, scheme):
    monkeypatch.setenv('OSS_ENDPOINT', f'{scheme}://oss.example.com')
    monkeypatch.setenv('PERSONAL_SKILLS_ALLOWED_OSS_HOSTS', 'runtime-test.oss.example.com')
    catalog, manifest = _skill_payloads(
        url=f'{scheme}://runtime-test.oss.example.com/skill.md?secret=token',
    )
    ctx, middleware, guard, body = await _build(
        personal_skills_catalog=catalog, personal_skills_access_manifest=manifest,
    )
    calls = []
    try:
        registry = current_personalization_context().registry
        async def fetch(url, _max_bytes):
            calls.append(True)
            return DownloadedPersonalSkill(body, url, False)
        registry._fetcher = fetch
        tool = await ctx.agent.toolkit.check_tool_available('activate_personal_skill', [])
        payload = {'skill_ref': 'personal:skill_001'}
        decision = await ctx.agent._engine.check_permission(tool, payload)
        assert decision.behavior == PermissionBehavior.ALLOW
        assert guard.calls == 1 and not calls
        async def execute():
            async for result in ctx.agent.toolkit.call_tool(call, ctx.agent.state):
                yield result
        call = ToolCallBlock(id='load', name='activate_personal_skill', input=json.dumps(payload))
        results = [r async for r in middleware.on_acting(ctx.agent, {'tool_call': call}, execute)]
        assert '汇总本月进展' in str(results[0].content)
        assert calls == [True]
        with pytest.raises(GatewayError) as rejected:
            _ = [r async for r in middleware.on_acting(ctx.agent, {'tool_call': call}, execute)]
        assert 'permit' in str(rejected.value.__cause__)
        assert calls == [True]
    finally:
        await BankRuntimePersonalizationCleanupHook().run(ctx)


@pytest.mark.asyncio
async def test_personal_loader_rejects_wrong_refs_parameters_and_guard_denial():
    ctx, _middleware, guard, _body = await _build()
    try:
        tool = await ctx.agent.toolkit.check_tool_available('activate_personal_skill', [])
        for payload in ({'skill_ref': 'personal:other-user'},
                        {'skill_ref': 'personal:skill_001', 'url': 'https://untrusted.example'}):
            decision = await ctx.agent._engine.check_permission(tool, payload)
            assert decision.behavior == PermissionBehavior.DENY
        assert guard.calls == 0
        guard.behavior = PermissionBehavior.DENY
        decision = await ctx.agent._engine.check_permission(tool, {'skill_ref': 'personal:skill_001'})
        assert decision.behavior == PermissionBehavior.DENY and guard.calls == 1
    finally:
        await BankRuntimePersonalizationCleanupHook().run(ctx)


@pytest.mark.asyncio
async def test_personal_loader_cannot_execute_after_registry_cleanup():
    ctx, middleware, _guard, _body = await _build()
    try:
        tool = await ctx.agent.toolkit.check_tool_available('activate_personal_skill', [])
        payload = {'skill_ref': 'personal:skill_001'}
        decision = await ctx.agent._engine.check_permission(tool, payload)
        assert decision.behavior == PermissionBehavior.ALLOW
        await BankRuntimePersonalizationCleanupHook().run(ctx)
        executed = []
        async def forbidden():
            executed.append(True)
            raise AssertionError('expired request must not execute')
            yield
        call = ToolCallBlock(id='expired', name='activate_personal_skill', input=json.dumps(payload))
        with pytest.raises(GatewayError) as rejected:
            _ = [r async for r in middleware.on_acting(ctx.agent, {'tool_call': call}, forbidden)]
        assert 'scope changed' in str(rejected.value.__cause__) and not executed
    finally:
        await BankRuntimePersonalizationCleanupHook().run(ctx)


@pytest.mark.asyncio
async def test_same_named_function_does_not_receive_personal_reader_admission():
    ctx, _middleware, guard, _body = await _build()
    try:
        async def impostor(skill_ref: str):
            return 'untrusted'
        tool = FunctionTool(impostor, name='activate_personal_skill')
        decision = await ctx.agent._engine.check_permission(tool, {'skill_ref': 'personal:skill_001'})
        assert decision.behavior == PermissionBehavior.DENY and guard.calls == 0
    finally:
        await BankRuntimePersonalizationCleanupHook().run(ctx)


@pytest.mark.asyncio
@pytest.mark.parametrize('overrides', [
    {'identity_json': None},
    {'personal_skills_catalog': None, 'personal_skills_access_manifest': None},
    {'personal_skills_access_manifest': {'schema_version': 'invalid'}},
])
async def test_invalid_or_absent_personal_context_has_no_model_loader(overrides):
    ctx, middleware, _guard, _body = await _build(**overrides)
    try:
        schemas = await ctx.agent.toolkit.get_tool_schemas()
        async def model(**kwargs):
            return kwargs['tools']
        assert await middleware.on_model_call(ctx.agent, {'tools': schemas}, model) == []
    finally:
        await BankRuntimePersonalizationCleanupHook().run(ctx)


@pytest.mark.asyncio
async def test_loader_replacement_after_guard_cannot_execute():
    ctx, middleware, _guard, _body = await _build()
    try:
        payload = {'skill_ref': 'personal:skill_001'}
        tool = await ctx.agent.toolkit.check_tool_available('activate_personal_skill', [])
        assert (await ctx.agent._engine.check_permission(tool, payload)).behavior == PermissionBehavior.ALLOW
        async def impostor(skill_ref: str):
            return 'untrusted'
        ctx.agent.toolkit.tool_groups[0].tools[:] = [FunctionTool(impostor, name=tool.name)]
        executed = []
        async def forbidden():
            executed.append(True)
            raise AssertionError('replacement must not execute')
            yield
        call = ToolCallBlock(id='swapped', name=tool.name, input=json.dumps(payload))
        with pytest.raises(GatewayError) as rejected:
            _ = [r async for r in middleware.on_acting(ctx.agent, {'tool_call': call}, forbidden)]
        assert 'scope changed' in str(rejected.value.__cause__) and not executed
    finally:
        await BankRuntimePersonalizationCleanupHook().run(ctx)


@pytest.mark.asyncio
async def test_reader_cannot_be_reused_in_another_request():
    first, middleware, guard, _body = await _build()
    second = None
    try:
        tool = await first.agent.toolkit.check_tool_available('activate_personal_skill', [])
        second, _other, _other_guard, _body = await _build()
        payload = {'skill_ref': 'personal:skill_001'}
        decision = await first.agent._engine.check_permission(tool, payload)
        assert decision.behavior == PermissionBehavior.DENY and guard.calls == 0
        assert not await middleware.personal_skills.visible()
    finally:
        if second is not None:
            await BankRuntimePersonalizationCleanupHook().run(second)
        await BankRuntimePersonalizationCleanupHook().run(first)


@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['execute_shell_command', 'artifact_generate', 'UnapprovedMCP__read'])
async def test_personal_context_does_not_authorize_business_tools(name):
    ctx, _middleware, guard, _body = await _build()
    try:
        tool = SimpleNamespace(name=name, is_external_tool=False)
        decision = await ctx.agent._engine.check_permission(tool, {})
        assert decision.behavior == PermissionBehavior.DENY and guard.calls == 0
    finally:
        await BankRuntimePersonalizationCleanupHook().run(ctx)


@pytest.mark.asyncio
@pytest.mark.parametrize('denied', [True, False])
async def test_personal_loader_honors_real_tool_guard(monkeypatch, denied):
    from unittest.mock import MagicMock
    from agentscope.permission import PermissionEngine, PermissionContext
    from qwenpaw.security.tool_guard.models import ToolGuardResult
    # The real AgentBuilder supplies this profile; lightweight fixtures normally omit it.
    monkeypatch.setattr('qwenpaw.config.config.load_agent_config', lambda _: SimpleNamespace(approval_level='auto'))
    ctx, middleware, _guard, _body = await _build(agent_id='assistant-a')
    engine = MagicMock()
    engine.enabled = True
    engine.is_denied.return_value = denied
    engine.guard.return_value = ToolGuardResult(tool_name='activate_personal_skill', params={})
    monkeypatch.setattr('qwenpaw.security.tool_guard.engine.get_guard_engine', lambda: engine)
    ctx.agent._engine.delegate = PermissionEngine(PermissionContext())
    try:
        decision = await ctx.agent._engine.check_permission(middleware.personal_skills.tool, {'skill_ref':'personal:skill_001'})
        assert decision.behavior == (PermissionBehavior.DENY if denied else PermissionBehavior.ALLOW)
        engine.guard.assert_called_once()
    finally:
        await BankRuntimePersonalizationCleanupHook().run(ctx)
