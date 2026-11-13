import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bank_runtime.execution_configuration import ExecutionConfiguration


def skill(root):
    root.mkdir()
    (root / 'references').mkdir()
    (root / 'SKILL.md').write_text('---\nname: review\ndescription: Review rules\n---\n# Review\n[Rules](references/rules.md)', encoding='utf-8')
    (root / 'references/rules.md').write_text('original', encoding='utf-8')
    return root


def test_snapshot_keeps_complete_skill_tree_config_and_provider(tmp_path):
    source = skill(tmp_path / 'review')
    config = SimpleNamespace(language='zh', running={'max_retries': 2})
    provider = SimpleNamespace(generate_kwargs={'temperature': 0.2})
    with ExecutionConfiguration(config, provider, ['review'], {'bank'}) as snapshot:
        frozen = Path(snapshot.skill_directories([str(source)])[0])
        before = snapshot.skill_versions()
        config.running['max_retries'] = 9
        provider.generate_kwargs['temperature'] = 1
        (source / 'references/rules.md').write_text('changed', encoding='utf-8')
        assert (frozen / 'references/rules.md').read_text() == 'original'
        assert snapshot.config.running['max_retries'] == 2
        assert snapshot.provider.generate_kwargs['temperature'] == 0.2
        assert snapshot.skill_versions() == before
        assert snapshot.skill_directories([str(source)]) == [str(frozen)]
    assert not frozen.exists()


def test_missing_reference_rejects_snapshot(tmp_path):
    source = skill(tmp_path / 'review')
    (source / 'references/rules.md').unlink()
    with ExecutionConfiguration(None, None, [], set()) as snapshot:
        with pytest.raises(ValueError, match='reference'):
            snapshot.skill_directories([str(source)])


def test_symlink_and_escaping_reference_are_rejected(tmp_path):
    source = skill(tmp_path / 'review')
    (source / 'references/link.md').symlink_to(tmp_path / 'outside')
    with ExecutionConfiguration(None, None, [], set()) as snapshot:
        with pytest.raises(ValueError, match='resource'):
            snapshot.skill_directories([str(source)])
    (source / 'references/link.md').unlink()
    (source / 'SKILL.md').write_text('[outside](../outside.md)', encoding='utf-8')
    with ExecutionConfiguration(None, None, [], set()) as snapshot:
        with pytest.raises(ValueError, match='reference'):
            snapshot.skill_directories([str(source)])


def test_snapshot_hash_is_independent_of_disposable_directory(tmp_path):
    source = skill(tmp_path / 'review')
    with ExecutionConfiguration(None, None, [], set()) as one, ExecutionConfiguration(None, None, [], set()) as two:
        first = one.skill_directories([str(source)])
        second = two.skill_directories([str(source)])
        assert first != second
        assert one.skill_versions() == two.skill_versions()
        assert one.normalize_paths(first[0]) == two.normalize_paths(second[0])


@pytest.mark.asyncio
async def test_toolkit_uses_verified_snapshot_even_if_live_skill_disappears(tmp_path, monkeypatch):
    from qwenpaw.runtime.builder import AgentBuilder
    source = skill(tmp_path / 'review')
    async def list_tools(**kwargs):
        return []
    with ExecutionConfiguration(None, None, ['review'], set()) as snapshot:
        snapshot.skill_directories([str(source)])
        (source / 'SKILL.md').unlink()
        ctx = SimpleNamespace(extras={'request_configuration': snapshot},
            workspace=SimpleNamespace(local_workspace=SimpleNamespace(list_tools=list_tools)))
        monkeypatch.setattr(AgentBuilder, '_resolve_skill_loader_dirs',
            lambda *args: pytest.fail('must not reopen live skills'))
        toolkit = await AgentBuilder().build_toolkit(None, ctx=ctx, effective_skills=['review'])
        prompt = await toolkit.get_skill_instructions()
        assert 'Review' in prompt or 'review' in prompt


@pytest.mark.asyncio
async def test_cleanup_releases_snapshot_even_when_session_prepare_failed(tmp_path):
    from bank_runtime.session import ManagedSessionCleanupHook
    source = skill(tmp_path / 'review')
    snapshot = ExecutionConfiguration(None, None, [], set())
    frozen = Path(snapshot.skill_directories([str(source)])[0])
    ctx = SimpleNamespace(extras={'request_configuration': snapshot})
    await ManagedSessionCleanupHook().run(ctx)
    assert not frozen.exists()
    assert 'request_configuration' not in ctx.extras


def test_prompt_is_not_reloaded_after_verification():
    from qwenpaw.runtime.builder import AgentBuilder
    with ExecutionConfiguration(None, None, [], set()) as snapshot:
        snapshot.base_prompt = 'Verified platform policy'
        ctx = SimpleNamespace(extras={'request_configuration': snapshot})
        assert AgentBuilder().build_prompt(ctx) == 'Verified platform policy'


def test_model_factory_uses_verified_provider_without_loading_live_configuration(monkeypatch):
    from qwenpaw.agents.model_factory import create_model_and_formatter
    from qwenpaw.config.config import AgentProfileConfig, ModelSlotConfig
    class ProviderReached(Exception):
        pass
    class Provider:
        def get_chat_model_instance(self, model):
            assert model == 'verified-model'
            raise ProviderReached()
    config = AgentProfileConfig(id='agent', name='Test',
        active_model=ModelSlotConfig(provider_id='verified-provider', model='verified-model'))
    monkeypatch.setattr('qwenpaw.config.config.load_agent_config', lambda *_: pytest.fail('live config read'))
    monkeypatch.setattr('qwenpaw.providers.ProviderManager.get_instance', lambda: pytest.fail('live provider read'))
    with pytest.raises(ProviderReached):
        create_model_and_formatter(agent_id='agent', agent_config_snapshot=config, provider_snapshot=Provider())


def test_snapshot_byte_limit_is_enforced_before_loading_skills(tmp_path, monkeypatch):
    source = skill(tmp_path / 'review')
    monkeypatch.setattr(ExecutionConfiguration, 'MAX_BYTES', 5)
    with ExecutionConfiguration(None, None, [], set()) as snapshot:
        with pytest.raises(ValueError, match='byte limit'):
            snapshot.skill_directories([str(source)])
