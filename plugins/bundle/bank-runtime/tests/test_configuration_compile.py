import base64
import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from bank_runtime.configuration_compile import compile_authoring_configuration
from bank_runtime.release import BANK_RUNTIME_PLUGIN_VERSION
from bank_runtime.release_manifest import build_manifest, canonical, sha256


@pytest.fixture
def authoring(tmp_path, monkeypatch):
    from qwenpaw.config.config import AgentProfileConfig, ModelSlotConfig
    from qwenpaw.providers.provider import ModelInfo
    from qwenpaw.agents.skill_system.store import get_workspace_skills_dir, get_workspace_skill_manifest_path
    monkeypatch.setenv('QWENPAW_BUILD_DIGEST','a'*64)
    monkeypatch.setenv('QWENPAW_NODE_ID','node-a')
    config = AgentProfileConfig(id='agent',name='Agent',active_model=ModelSlotConfig(provider_id='p',model='m'),
        system_prompt_files=['AGENTS.md','PROFILE.md','SOUL.md'])
    (tmp_path/'agent.json').write_text(config.model_dump_json())
    (tmp_path/'SOUL.md').write_text('Must never enter this release')
    skill_root = get_workspace_skills_dir(tmp_path)
    (skill_root/'review/references').mkdir(parents=True)
    (skill_root/'review/SKILL.md').write_text('---\nname: review\ndescription: Review\n---\n[Rules](references/rules.md)')
    (skill_root/'review/references/rules.md').write_text('Edited rules')
    get_workspace_skill_manifest_path(tmp_path).write_text(json.dumps({'skills':{
        'review':{'enabled':True,'channels':['bank-runtime']},'disabled':{'enabled':False}}}))
    monkeypatch.setattr('qwenpaw.config.utils.load_config',lambda:SimpleNamespace(agents=SimpleNamespace(
        profiles={'agent':SimpleNamespace(id='agent',enabled=True,workspace_dir=str(tmp_path))})))
    info = ModelInfo(id='m',name='M',max_input_length=32768,max_input_length_configured=True,max_tokens=1024)
    provider = SimpleNamespace(api_key='NEVER_EXPORT',base_url='https://private-provider',
        get_model_info=lambda _:info,get_effective_generate_kwargs=lambda _:{'max_tokens':1024,'temperature':0.2})
    monkeypatch.setattr('qwenpaw.providers.ProviderManager.get_instance',lambda:SimpleNamespace(get_provider=lambda _:provider))
    policy = {'agent':{'language':'zh','approval_level':'AUTO','active_model':{'provider_id':'p','model':'m'},'running':{}},
        'prompt':'Governed prompt','tool_names':['bank_assistant'],'model':{'model_info':info.model_dump(mode='json'),
        'generate_kwargs':{'max_tokens':1024}}}
    manifest=build_manifest('agent',policy,{'worker_build':'a'*64,'plugin_version':BANK_RUNTIME_PLUGIN_VERSION,
        'python_packages':{}},{})
    return manifest, skill_root


@pytest.mark.asyncio
async def test_current_authoring_becomes_complete_package_without_native_personality_or_credentials(authoring):
    manifest, root = authoring
    result = await compile_authoring_configuration('agent',node_id='node-a',base_manifest=manifest)
    package = result['package']
    assert package['policy']['prompt'] == 'Governed prompt'
    assert package['policy']['tool_names'] == ['bank_assistant']
    assert package['policy']['model']['generate_kwargs']['temperature'] == 0.2
    assert base64.b64decode(package['files']['skills/review/references/rules.md']) == b'Edited rules'
    assert 'NEVER_EXPORT' not in json.dumps(result) and 'Must never enter' not in json.dumps(result)
    assert result['base_release_id'] == sha256(canonical(manifest))
    assert result['node_id'] == 'node-a'
    from bank_runtime.config_release import ConfigReleaseCache
    files={path:base64.b64decode(value) for path,value in package['files'].items()}
    compiled=build_manifest('agent',package['policy'],package['dependencies'],files,package['executable'])
    receipt=await ConfigReleaseCache(root.parent/'cache').install('agent',{
        'release_id':result['release_id'],'manifest':compiled,
        'resources':{sha256(value):base64.b64encode(value).decode() for value in files.values()}})
    assert receipt['loaded'] is True


@pytest.mark.asyncio
async def test_missing_resources_or_wrong_node_fail_closed(authoring):
    manifest, root = authoring
    with pytest.raises(ValueError):
        await compile_authoring_configuration('agent',node_id='other',base_manifest=manifest)
    (root/'review/references/rules.md').unlink()
    with pytest.raises(ValueError):
        await compile_authoring_configuration('agent',node_id='node-a',base_manifest=manifest)


@pytest.mark.asyncio
async def test_source_edit_during_snapshot_is_rejected(authoring, monkeypatch):
    from bank_runtime.execution_configuration import ExecutionConfiguration
    manifest, root = authoring
    original=ExecutionConfiguration.skill_directories
    def change(self,*args):
        result=original(self,*args)
        (root/'review/references/rules.md').write_text('changed after copy')
        return result
    monkeypatch.setattr(ExecutionConfiguration,'skill_directories',change)
    with pytest.raises(ValueError):
        await compile_authoring_configuration('agent',node_id='node-a',base_manifest=manifest)


def test_compile_http_route_enforces_real_native_auth_bridge(authoring,monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from qwenpaw.app import auth
    from qwenpaw.config.utils import load_config
    from bank_runtime.management_auth import install_native_auth_bridge
    from bank_runtime.config_release import build_config_release_router
    manifest,_=authoring
    monkeypatch.setattr(auth,'is_auth_enabled',lambda:True)
    monkeypatch.setattr(auth,'has_registered_users',lambda:True)
    monkeypatch.setattr(auth,'verify_token',lambda _:None)
    monkeypatch.setenv('QWENPAW_SERVICE_TOKEN','synthetic-service')
    monkeypatch.setattr('bank_runtime.production_guard.require_production_readiness',lambda:None)
    monkeypatch.setattr('bank_runtime.content_review.load_config',load_config)
    app=FastAPI()
    app.add_middleware(auth.AuthMiddleware)
    install_native_auth_bridge(app)
    app.include_router(build_config_release_router(),prefix='/api/bank-runtime')
    path='/api/bank-runtime/agents/agent/config-releases/compile'
    with TestClient(app) as client:
        assert client.post(path,json={}).status_code == 401
        headers={'Authorization':'Bearer synthetic-service','X-Agent-Id':'agent'}
        assert client.get(path,headers=headers).status_code == 401
        assert client.post(path,headers=headers,json={'node_id':'wrong','base_manifest':manifest}).status_code == 422
        response=client.post(path,headers=headers,json={'node_id':'node-a','base_manifest':manifest})
        assert response.status_code == 200,response.text
        assert response.json()['node_id'] == 'node-a'
