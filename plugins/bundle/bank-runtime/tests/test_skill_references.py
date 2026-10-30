"""Real Skill calls keep references scoped to the visible skill."""
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from agentscope.message import ToolCallBlock
from agentscope.permission import PermissionBehavior
from agentscope.skill import Skill
from agentscope.state import AgentState
from agentscope.tool import Toolkit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bank_runtime.gateway.middleware import BankRuntimeGatewayMiddleware, GatewayPermissionEngine
from test_native_skill_boundary import DenyingClient, Guard


def fixture(tmp_path):
    root = tmp_path / 'delivery'
    (root / 'references').mkdir(parents=True)
    (root / 'references' / 'format.md').write_text('Format contract, not execution authority.')
    skill = Skill('delivery', 'Deliver files', str(root),
                  'Common workflow. [Format](references/format.md)', 0)
    agent = SimpleNamespace(toolkit=Toolkit(skills_or_loaders=[skill]), state=AgentState())
    middleware = BankRuntimeGatewayMiddleware(DenyingClient())
    middleware.bind_native_skills(agent)
    engine = GatewayPermissionEngine(Guard(), middleware)
    return root, skill, agent, middleware, engine


async def execute(agent, middleware, payload):
    call = ToolCallBlock(id='reference-read', name='Skill', input=json.dumps(payload))
    async def next_handler():
        async for item in agent.toolkit.call_tool(call, agent.state):
            yield item
    return [item async for item in middleware.on_acting(agent, {'tool_call': call}, next_handler)]


@pytest.mark.asyncio
async def test_main_and_reference_are_separate_real_tool_reads(tmp_path):
    root, skill, agent, middleware, engine = fixture(tmp_path)
    viewer = agent.toolkit.builtin_skill_viewer.tool
    assert 'reference' in viewer.input_schema['properties']
    for payload, expected in [({'skill': 'delivery'}, 'Common workflow.'),
                              ({'skill': 'delivery', 'reference': 'references/format.md'}, 'Format contract')]:
        assert (await engine.check_permission(viewer, payload)).behavior == PermissionBehavior.ALLOW
        result = await execute(agent, middleware, payload)
        assert expected in result[-1].content[0].text
    with pytest.raises(Exception):
        await execute(agent, middleware, payload)
    assert skill.markdown == 'Common workflow. [Format](references/format.md)'


@pytest.mark.asyncio
@pytest.mark.parametrize('reference', ['../secret.md', '/etc/passwd', 'references/../secret.md',
    'references//format.md', 'references/./format.md', 'references\\format.md',
    'scripts/run.py', 'references/missing.md', 'references/unlisted.md',
    'references/%2e%2e/secret.md', '', None, 7])
async def test_invalid_or_unlisted_reference_is_denied(tmp_path, reference):
    root, skill, agent, middleware, engine = fixture(tmp_path)
    (root / 'references' / 'unlisted.md').write_text('not linked')
    assert (await engine.check_permission(agent.toolkit.builtin_skill_viewer.tool,
        {'skill': 'delivery', 'reference': reference})).behavior == PermissionBehavior.DENY


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['symlink', 'directory_link', 'hardlink', 'fifo', 'large', 'invalid_utf8'])
async def test_reference_rejects_nonregular_or_unbounded_files(tmp_path, kind):
    root, skill, agent, middleware, engine = fixture(tmp_path)
    target = root / 'references' / 'format.md'
    target.unlink()
    secret = tmp_path / 'secret.md'
    secret.write_text('private')
    if kind == 'symlink':
        target.symlink_to(secret)
    elif kind == 'directory_link':
        (root / 'references').rmdir()
        (root / 'references').symlink_to(tmp_path, target_is_directory=True)
        (tmp_path / 'format.md').write_text('outside')
    elif kind == 'hardlink':
        os.link(secret, target)
    elif kind == 'fifo':
        os.mkfifo(target)
    elif kind == 'large':
        target.write_bytes(b'x' * 65537)
    else:
        target.write_bytes(b'\xff')
    assert (await engine.check_permission(agent.toolkit.builtin_skill_viewer.tool,
        {'skill': 'delivery', 'reference': 'references/format.md'})).behavior == PermissionBehavior.DENY


@pytest.mark.asyncio
async def test_removal_after_admission_fails_closed(tmp_path):
    root, skill, agent, middleware, engine = fixture(tmp_path)
    payload = {'skill': 'delivery', 'reference': 'references/format.md'}
    viewer = agent.toolkit.builtin_skill_viewer.tool
    assert (await engine.check_permission(viewer, payload)).behavior == PermissionBehavior.ALLOW
    (root / 'references' / 'format.md').unlink()
    with pytest.raises(Exception):
        await execute(agent, middleware, payload)
    assert (await engine.check_permission(SimpleNamespace(name='Skill'), payload)).behavior == PermissionBehavior.DENY


@pytest.mark.asyncio
async def test_guard_cannot_change_admitted_reference(tmp_path):
    root, skill, agent, middleware, _ = fixture(tmp_path)
    class MutatingGuard(Guard):
        async def check_permission(self, tool, payload):
            decision = await super().check_permission(tool, payload)
            payload['reference'] = '../secret.md'
            return decision
    engine = GatewayPermissionEngine(MutatingGuard(), middleware)
    assert (await engine.check_permission(agent.toolkit.builtin_skill_viewer.tool,
        {'skill': 'delivery', 'reference': 'references/format.md'})).behavior == PermissionBehavior.DENY


@pytest.mark.asyncio
@pytest.mark.parametrize('model_id', ['deepseek-v4-flash', 'qwen3-27b'])
async def test_reference_schema_and_result_survive_actual_sdk_formatting(tmp_path, monkeypatch, model_id):
    import copy
    from unittest.mock import AsyncMock
    from agentscope.credential._openai import OpenAICredential
    from agentscope.message import AssistantMsg, SystemMsg, UserMsg, ToolResultBlock, ToolResultState
    from openai.types.chat import ChatCompletion
    from qwenpaw.providers.openai_chat_model_compat import OpenAIChatModelCompat

    root, skill, agent, middleware, engine = fixture(tmp_path)
    middleware.model_reliability = None
    payload = {'skill': 'delivery', 'reference': 'references/format.md'}
    viewer = agent.toolkit.builtin_skill_viewer.tool
    assert (await engine.check_permission(viewer, payload)).behavior == PermissionBehavior.ALLOW
    result = await execute(agent, middleware, payload)
    history = [SystemMsg('system', 'Policy'), UserMsg('user', '继续按确认的格式交付'),
               AssistantMsg('assistant', [ToolCallBlock(id='reference-read', name='Skill', input=json.dumps(payload)),
                   ToolResultBlock(id='reference-read', name='Skill', state=ToolResultState.SUCCESS,
                                   output=result[-1].content[0].text)])]
    before = copy.deepcopy(history)
    seen = []

    async def api(*, messages, tools, **kwargs):
        assert messages[0]['role'] == 'system'
        assert all(message['role'] != 'system' for message in messages[1:])
        function = tools[0]['function']
        assert function['parameters']['required'] == ['skill']
        assert function['parameters']['properties']['reference']['type'] == 'string'
        calls = [call for message in messages for call in message.get('tool_calls', [])]
        assert json.loads(calls[0]['function']['arguments']) == payload
        outputs = [message for message in messages if message['role'] == 'tool']
        assert outputs[0]['tool_call_id'] == calls[0]['id'] == 'reference-read'
        assert 'Format contract' in str(outputs[0]['content'])
        seen.append(messages)
        return ChatCompletion(id='probe', created=0, model=model_id, object='chat.completion',
            choices=[{'index': 0, 'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': '协议检查'}}])

    monkeypatch.setattr('openai.AsyncClient', lambda **kwargs: SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(side_effect=api)))))
    model = OpenAIChatModelCompat(credential=OpenAICredential(
        id='probe', api_key='unused', base_url='http://127.0.0.1:1/v1'), model=model_id, stream=False)

    async def invoke(**kwargs):
        return await model._call_api(model_id, kwargs['messages'], kwargs['tools'])

    schemas = await agent.toolkit.get_tool_schemas(agent.state.tool_context.activated_groups)
    await middleware.on_model_call(agent, {'messages': history, 'tools': schemas}, invoke)
    assert len(seen) == 1 and history == before
