import base64
import copy
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bank_runtime.config_release import ConfigReleaseCache
from bank_runtime.release_manifest import build_manifest, canonical, sha256
from bank_runtime.release import BANK_RUNTIME_PLUGIN_VERSION


@pytest.fixture
def bundle(monkeypatch):
    monkeypatch.setenv('QWENPAW_BUILD_DIGEST', 'a'*64)
    monkeypatch.setenv('QWENPAW_NODE_ID', 'node-a')
    policy = {'agent': {'language': 'zh', 'approval_level': 'AUTO', 'running': {},
        'active_model': {'provider_id': 'p', 'model': 'm'}}, 'prompt': 'Published policy',
        'tool_names': ['bank_assistant'], 'model': {'generate_kwargs': {'max_tokens': 1024, 'temperature': 0.1},
        'model_info': {'id': 'm', 'name': 'M', 'max_input_length': 32768,
                       'max_input_length_configured': True, 'max_tokens': 1024}}}
    files = {'skills/review/SKILL.md': b'---\nname: review\ndescription: Review\n---\n[Rules](references/rules.md)',
             'skills/review/references/rules.md': b'original'}
    manifest = build_manifest('agent', policy, {'worker_build': 'a'*64,
        'plugin_version': BANK_RUNTIME_PLUGIN_VERSION, 'python_packages': {}}, files)
    from qwenpaw.providers.provider import ModelInfo
    provider = SimpleNamespace(models=[ModelInfo(id='m', name='Live')], extra_models=[], generate_kwargs={})
    monkeypatch.setattr('qwenpaw.providers.ProviderManager.get_instance',
        lambda: SimpleNamespace(get_provider=lambda _: provider))
    return manifest, files


def wire(manifest, files):
    return {'release_id': sha256(canonical(manifest)), 'manifest': manifest,
        'resources': {sha256(content): base64.b64encode(content).decode('ascii') for content in files.values()}}


@pytest.mark.asyncio
async def test_verified_release_is_loaded_with_complete_references_and_pinned_model(tmp_path, bundle):
    manifest, files = bundle
    cache = ConfigReleaseCache(tmp_path)
    result = await cache.install('agent', wire(manifest, files))
    loaded = cache.load('agent', result['release_id'])
    assert loaded.config.system_prompt_files == []
    assert loaded.config.active_model.model == 'm'
    assert loaded.provider.generate_kwargs['temperature'] == 0.1
    assert loaded.provider.models[0].max_input_length == 32768
    assert (Path(loaded.skill_directories[0])/'references/rules.md').read_text() == 'original'
    assert await cache.install('agent', wire(manifest, files)) == result


@pytest.mark.asyncio
async def test_missing_dependency_never_acknowledges_loaded(tmp_path, bundle):
    manifest, files = bundle
    manifest['dependencies']['python_packages'] = {'nonexistent-bank-test-package': '1.0'}
    cache = ConfigReleaseCache(tmp_path)
    with pytest.raises(ValueError, match='dependency'):
        await cache.install('agent', wire(manifest, files))
    assert not list(tmp_path.rglob('manifest.json'))


@pytest.mark.asyncio
async def test_native_loader_failure_does_not_acknowledge_incomplete_skill(tmp_path, bundle):
    manifest, files = bundle
    files['skills/review/SKILL.md'] = b'# Missing required skill metadata'
    manifest = build_manifest('agent', manifest['policy'], manifest['dependencies'], files)
    with pytest.raises(ValueError, match='skill'):
        await ConfigReleaseCache(tmp_path).install('agent', wire(manifest, files))
    assert not list(tmp_path.rglob('manifest.json'))


@pytest.mark.asyncio
async def test_cross_agent_and_bad_resource_hash_are_rejected(tmp_path, bundle):
    manifest, files = bundle
    cache = ConfigReleaseCache(tmp_path)
    with pytest.raises(ValueError):
        await cache.install('other', wire(manifest, files))
    payload = wire(manifest, files)
    key = next(iter(payload['resources']))
    payload['resources'][key] = base64.b64encode(b'bad').decode('ascii')
    with pytest.raises(ValueError):
        await cache.install('agent', payload)


@pytest.mark.asyncio
async def test_corrupted_disposable_cache_fails_closed(tmp_path, bundle):
    manifest, files = bundle
    cache = ConfigReleaseCache(tmp_path)
    release = await cache.install('agent', wire(manifest, files))
    loaded = cache.load('agent', release['release_id'])
    resource = Path(loaded.skill_directories[0])/'references/rules.md'
    resource.chmod(0o600)
    resource.write_text('corrupted')
    with pytest.raises(ValueError):
        cache.load('agent', release['release_id'])


@pytest.mark.asyncio
async def test_two_releases_coexist_without_mutating_previous_resources(tmp_path, bundle):
    manifest, files = bundle
    cache = ConfigReleaseCache(tmp_path)
    first = await cache.install('agent', wire(manifest, files))
    newer = {**files, 'skills/review/references/rules.md': b'new rules'}
    manifest = build_manifest('agent', manifest['policy'], manifest['dependencies'], newer)
    second = await cache.install('agent', wire(manifest, newer))
    assert first['release_id'] != second['release_id']
    for release, expected in [(first, 'original'), (second, 'new rules')]:
        loaded = cache.load('agent', release['release_id'])
        assert (Path(loaded.skill_directories[0])/'references/rules.md').read_text() == expected


@pytest.mark.asyncio
async def test_budget_and_execution_use_frozen_release_after_live_configuration_changes(tmp_path, bundle, monkeypatch):
    from bank_runtime.context_budget import compile_budget, verify_execution_budget
    from qwenpaw.runtime.builder import AgentBuilder
    manifest, files = bundle
    cache = ConfigReleaseCache(tmp_path/'cache')
    receipt = await cache.install('agent', wire(manifest, files))
    original_load = cache.load
    def load(agent, release_id):
        loaded = original_load(agent, release_id)
        loaded.provider.get_model_info = lambda _: loaded.provider.models[0]
        loaded.provider.get_effective_generate_kwargs = lambda _: dict(loaded.provider.generate_kwargs)
        return loaded
    monkeypatch.setattr(cache, 'load', load)
    monkeypatch.setattr('bank_runtime.config_release.configured_cache', lambda: cache)
    monkeypatch.setattr('qwenpaw.config.config.load_agent_config', lambda _: pytest.fail('live agent config read'))
    monkeypatch.setattr('qwenpaw.agents.skill_system.registry.resolve_effective_skills', lambda *args: pytest.fail('live skills read'))
    monkeypatch.setattr(AgentBuilder, 'build_prompt', lambda *args: 'Stable bank instructions')
    async def list_tools(**kwargs): return []
    workspace = SimpleNamespace(workspace_dir=tmp_path/'workspace', driver_manager=None,
        local_workspace=SimpleNamespace(list_tools=list_tools), plugins=SimpleNamespace(active_mode_names=lambda _:set()))
    payload = {'input_text':'Review approved facts', 'runtime_config_release':receipt['release_id'],
        'runtime_worker_node_id':'node-a', 'runtime_tool_visibility':{'authoritative':True,
        'worker_type':'qwenpaw','binding_snapshot_hash':'sha256:'+'a'*64,'worker_tool_names':['bank_assistant']}}
    budget = await compile_budget('agent', payload, workspace)
    context = SimpleNamespace(request=SimpleNamespace(**payload), agent_id='agent', workspace=workspace, extras={})
    execution = SimpleNamespace(context_package={'budget_evidence':{'model_policy_hash':budget['policy_hash'],
        'context_limit':budget['context_limit'],'output_reserve_tokens':budget['output_reserve_tokens'],
        'config_release_id':receipt['release_id']}})
    await verify_execution_budget(context, execution)
    snapshot = context.extras['request_configuration']
    try:
        cached = Path(original_load('agent', receipt['release_id']).skill_directories[0])/'references/rules.md'
        cached.chmod(0o600)
        cached.write_text('A later local change')
        toolkit = await AgentBuilder().build_toolkit(snapshot.config, agent_id='agent', effective_skills=['different-live-skill'],
            ctx=context, workspace_dir=workspace.workspace_dir)
        skills = await toolkit._get_available_skills([])
        assert list(skills) == ['review']
        assert (Path(snapshot.skill_directories()[0])/'references/rules.md').read_text() == 'original'
        assert snapshot.provider.generate_kwargs['temperature'] == 0.1
        assert snapshot.base_prompt.endswith('Published policy')
    finally:
        snapshot.close()
