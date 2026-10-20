"""Personal overlays survive request projection and the real provider formatter."""
import copy
import pytest
from agentscope.message import AssistantMsg, SystemMsg, UserMsg, ToolCallBlock, ToolResultBlock, ToolResultState
from agentscope.formatter import OpenAIChatFormatter
from test_personalization import _ctx, _request, _skill_bytes, _skill_payloads
from bank_runtime.personal_skills import DownloadedPersonalSkill, activate_personal_skill
from bank_runtime.personalization import (
    BankRuntimePersonalizationHook, BankRuntimePersonalizationCleanupHook,
    BankRuntimePersonalizationRedactionHook, current_personalization_context,
)
from bank_runtime.model_context import prepare_public_model_context
from qwenpaw.providers.chat_message_order import normalize_system_messages


def test_profile_compilation_keeps_empty_and_unknown_fields_out():
    from bank_runtime.personalization import _compile_profile
    def profile(preferences):
        return {'user_overlay': {'profile': {'schema_version': '1.0', 'trust_level': 'low', 'preferences': preferences}}}
    assert _compile_profile(profile({})) == ''
    assert _compile_profile(profile({'language': 'grant shell', 'response_style': 'ignore permission', 'new_instruction': 'override'})) == ''
    assert '使用简体中文回答。' in _compile_profile(profile({'language': 'zh-CN'}))
    assert 'Keep the answer brief' in _compile_profile(profile({'response_style': 'concise'}))


@pytest.mark.asyncio
async def test_profile_catalog_and_activated_body_reach_wire_and_are_cleaned(monkeypatch):
    monkeypatch.setenv('PERSONAL_SKILLS_ALLOWED_OSS_HOSTS', 'oss.example.com')
    body = _skill_bytes(body='# Synthetic monthly method\n\nUse headings Progress, Blockers, Next steps.\n')
    catalog, manifest = _skill_payloads(body)
    ctx = _ctx(_request(
        runtime_context={'user_overlay': {'profile': {'schema_version': '1.0', 'trust_level': 'low',
            'preferences': {'language': 'en-US', 'response_style': 'detailed', 'preferred_formats': ['list']}}}},
        personal_skills_catalog=catalog, personal_skills_access_manifest=manifest))
    await BankRuntimePersonalizationHook().run(ctx)
    registry = current_personalization_context().registry
    try:
        calls = []
        async def fetch(url, max_bytes):
            calls.append(True)
            return DownloadedPersonalSkill(body, url, False)
        registry._fetcher = fetch
        initial = {'messages': [SystemMsg('system', ctx.agent._system_prompt), UserMsg('user', '请用中文整理月报')], 'tools': []}
        first = prepare_public_model_context(initial)
        wire = str(normalize_system_messages(await OpenAIChatFormatter().format(first['messages'])))
        assert 'language: en-US' in wire and 'response_style: detailed' in wire
        assert 'Answer in English.' in wire
        assert 'personal:skill_001' in wire and 'Progress, Blockers, Next steps' not in wire
        assert 'secret=token' not in wire and not calls
        activated = await activate_personal_skill('personal:skill_001')
        message = AssistantMsg('assistant', [
            ToolCallBlock(id='personal-load', name='activate_personal_skill', input='{"skill_ref":"personal:skill_001"}'),
            ToolResultBlock(id='personal-load', name='activate_personal_skill', state=ToolResultState.SUCCESS, output=activated)])
        request = {**first, 'messages': [*first['messages'], message]}
        before = copy.deepcopy(request)
        prepared = prepare_public_model_context(request)
        wire = str(normalize_system_messages(await OpenAIChatFormatter().format(prepared['messages'])))
        assert 'Progress, Blockers, Next steps' in wire
        assert 'language: en-US' in wire and 'preferred_formats: list' in wire
        assert '请用中文整理月报' in wire and '当前用户要求优先' in wire
        assert wire.count('本轮回答约定') == 1 and len(calls) == 1
        assert request == before and prepared['messages'][-2] is message
        ctx.agent.state['state']['context'] = [{'content': activated}]
        await BankRuntimePersonalizationRedactionHook().run(ctx)
        assert 'Progress, Blockers, Next steps' not in str(ctx.agent.state_dict())
    finally:
        await BankRuntimePersonalizationCleanupHook().run(ctx)
    assert registry.closed and current_personalization_context() is None
    second = _ctx(_request(user_id='other-user', identity_json=None))
    await BankRuntimePersonalizationHook().run(second)
    try:
        assert 'personal:skill_001' not in second.agent._system_prompt
        assert 'language: en-US' not in second.agent._system_prompt
        assert await activate_personal_skill('personal:skill_001') == 'Personal Skill is unavailable for this request.'
    finally:
        await BankRuntimePersonalizationCleanupHook().run(second)
