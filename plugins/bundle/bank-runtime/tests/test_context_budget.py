import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bank_runtime.context_budget import configured_budget, attachment_input_bound, policy_digest


def test_unknown_window_is_unavailable_and_output_reserve_matches_provider():
    info = SimpleNamespace(max_input_length=131072, max_input_length_configured=False, max_tokens=8192)
    with pytest.raises(ValueError, match='configured'):
        configured_budget(info, {})
    info.max_input_length_configured = True
    assert configured_budget(info, {}) == (131072, 8192)
    assert configured_budget(info, {'max_tokens': 12000}) == (131072, 12000)


def test_attachment_body_bounds_and_unknown_media_are_not_silently_zero():
    assert attachment_input_bound([{'display_name': 'data.txt', 'size_bytes': 100}], False) >= 400
    assert attachment_input_bound([{'display_name': 'data.xlsx', 'size_bytes': 100000}], True) > 0
    with pytest.raises(ValueError, match='media'):
        attachment_input_bound([{'display_name': 'image.png', 'content_type': 'image/png'}], False)


def test_policy_hash_ignores_current_request_but_tracks_real_policy_material():
    policy = dict(model='qwen', context_limit=131072, system='rules', skills='catalog', tools=[{'name': 'a'}])
    assert policy_digest(policy) == policy_digest(dict(reversed(list(policy.items()))))
    assert policy_digest(policy) != policy_digest({**policy, 'system': 'updated rules'})


@pytest.mark.asyncio
async def test_real_schema_compilation_never_builds_agent_and_counts_input(monkeypatch, tmp_path):
    from bank_runtime.context_budget import compile_budget
    from qwenpaw.config.config import AgentProfileConfig, ModelSlotConfig
    config = AgentProfileConfig(id='agent-a', name='Budget', active_model=ModelSlotConfig(provider_id='p', model='m'))
    info = SimpleNamespace(max_input_length=131072, max_input_length_configured=True, max_tokens=8192)
    provider = SimpleNamespace(get_model_info=lambda _: info, get_effective_generate_kwargs=lambda _: {})
    manager = SimpleNamespace(get_provider=lambda _: provider)
    monkeypatch.setattr('qwenpaw.config.config.load_agent_config', lambda _: config)
    monkeypatch.setattr('qwenpaw.providers.ProviderManager.get_instance', lambda: manager)
    monkeypatch.setattr('qwenpaw.agents.skill_system.registry.resolve_effective_skills', lambda *args: [])
    monkeypatch.setattr('qwenpaw.runtime.builder.AgentBuilder.build', lambda *args: pytest.fail('must not construct execution agent'))
    monkeypatch.setattr('qwenpaw.runtime.builder.AgentBuilder.build_prompt', lambda *args: 'FIXED POLICY')
    async def list_tools(**kwargs):
        return []
    workspace = SimpleNamespace(workspace_dir=tmp_path, driver_manager=None,
        local_workspace=SimpleNamespace(list_tools=list_tools), plugins=SimpleNamespace(active_mode_names=lambda _: set()))
    payload = {'input_text': '你好', 'runtime_tool_visibility': {'authoritative': True, 'worker_type': 'qwenpaw',
        'binding_snapshot_hash': 'sha256:' + 'a' * 64, 'worker_tool_names': []}}
    first = await compile_budget('agent-a', payload, workspace)
    second = await compile_budget('agent-a', {**payload, 'input_text': '你好' * 30, 'task_id': 'another'}, workspace)
    assert first['context_limit_source'] == 'configured_model'
    assert first['tool_schema_tokens'] > 1000
    assert second['input_tokens'] > first['input_tokens']
    assert first['policy_hash'] == second['policy_hash']
    config.workspace_dir = '/different-node/workspace'
    config.name = 'Node-local display name'
    relocated = await compile_budget('agent-a', payload, workspace)
    assert relocated['policy_hash'] == first['policy_hash']
    captured = {}
    third = await compile_budget('agent-a', payload, workspace, configuration_sink=captured)
    snapshot = captured['configuration']
    try:
        config.running.llm_max_retries += 1
        assert snapshot.config.running.llm_max_retries != config.running.llm_max_retries
        fourth = await compile_budget('agent-a', payload, workspace)
        assert fourth['policy_hash'] != third['policy_hash']
    finally:
        snapshot.close()


def test_skill_reference_content_changes_policy_directory_hash(tmp_path):
    from bank_runtime.context_budget import skill_directory_digest
    (tmp_path / 'SKILL.md').write_text('# Skill', encoding='utf-8')
    refs = tmp_path / 'references'
    refs.mkdir()
    (refs / 'rule.md').write_text('initial', encoding='utf-8')
    before = skill_directory_digest(tmp_path)
    (refs / 'rule.md').write_text('updated', encoding='utf-8')
    assert skill_directory_digest(tmp_path) != before


@pytest.mark.asyncio
async def test_execution_node_policy_drift_rejected_before_agent_build(monkeypatch):
    from bank_runtime.context_budget import verify_execution_budget
    from bank_runtime.remote_state import RemoteStateError
    async def compiled(*args, **kwargs):
        return {'policy_hash': 'b' * 64, 'context_limit': 131072, 'output_reserve_tokens': 8192}
    monkeypatch.setattr('bank_runtime.context_budget.compile_budget', compiled)
    ctx = SimpleNamespace(request=SimpleNamespace(input_text='query'), agent_id='a', workspace=object())
    execution = SimpleNamespace(context_package={'budget_evidence': {'model_policy_hash': 'a' * 64,
        'context_limit': 131072, 'output_reserve_tokens': 8192}})
    with pytest.raises(RemoteStateError) as error:
        await verify_execution_budget(ctx, execution)
    assert error.value.error_code == 'WORKER_POLICY_MISMATCH'
