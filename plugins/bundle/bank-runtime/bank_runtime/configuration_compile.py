"""Compile explicit authoring changes into a draft; never publish or run an Agent."""
import base64
import copy
from importlib.metadata import version
import json
import os
from pathlib import Path
import stat

from .execution_configuration import ExecutionConfiguration
from .release import BANK_RUNTIME_PLUGIN_VERSION
from .release_manifest import (MAX_MANIFEST_BYTES, build_manifest, canonical, identifier,
    sha256, validate_manifest)


def _read(path):
    if any(parent.is_symlink() for parent in (path, *path.parents)) or not stat.S_ISREG(path.stat().st_mode):
        raise ValueError('Invalid authoring configuration source')
    with path.open('rb') as stream:
        raw = stream.read(MAX_MANIFEST_BYTES+1)
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ValueError('Authoring configuration exceeds limit')
    return raw


def _model_policy(config):
    from qwenpaw.providers import ProviderManager
    from .context_budget import configured_budget
    manager = ProviderManager.get_instance()
    slot = config.active_model or manager.get_active_model()
    if slot is None:
        raise ValueError('Authoring model is not configured')
    provider = copy.deepcopy(manager.get_provider(slot.provider_id))
    if provider is None:
        raise ValueError('Authoring provider is not configured')
    info = provider.get_model_info(slot.model)
    defaults = provider.get_effective_generate_kwargs(slot.model)
    configured_budget(info, defaults)
    return slot.model_dump(mode='json'), {'model_info':info.model_dump(mode='json'), 'generate_kwargs':defaults}


async def compile_authoring_configuration(agent_id, *, node_id, base_manifest):
    from qwenpaw.config.utils import load_config
    from qwenpaw.config.config import AgentProfileConfig
    from .context_budget import skill_directory_digest
    identifier(agent_id)
    if identifier(node_id) != os.environ.get('QWENPAW_NODE_ID'):
        raise ValueError('Authoring node identity mismatch')
    base_manifest = validate_manifest(copy.deepcopy(base_manifest))
    if base_manifest['agent_id'] != agent_id:
        raise ValueError('Authoring base agent mismatch')
    profile = load_config().agents.profiles.get(agent_id)
    if profile is None or profile.id != agent_id or not profile.enabled:
        raise ValueError('Authoring agent unavailable')
    root = Path(profile.workspace_dir).expanduser().absolute()
    # Bypass native loaders which may create defaults, migrate configuration,
    # rename legacy skill directories, or import access-control state on read.
    config_raw, manifest_raw = _read(root/'agent.json'), _read(root/'skill.json')
    config_source, skill_manifest = json.loads(config_raw), json.loads(manifest_raw)
    if (not isinstance(config_source,dict) or config_source.get('id') != agent_id
            or not isinstance(skill_manifest,dict)):
        raise ValueError('Invalid authoring documents')
    config = AgentProfileConfig(id=agent_id,name=agent_id,system_prompt_files=[],
        **{key:config_source[key] for key in ('language','approval_level','active_model') if key in config_source})
    slot, model = _model_policy(config)
    policy = copy.deepcopy(base_manifest['policy'])
    policy['agent'].update(config.model_dump(mode='json',include={'language','approval_level'}) | {'active_model':slot})
    policy['model'] = model
    # Prompt and capability ceiling belong to Runtime governance. Native
    # personality markdown, credentials and arbitrary tools never enter here.
    dependencies = {'worker_build':os.environ.get('QWENPAW_BUILD_DIGEST',''),
        'plugin_version':BANK_RUNTIME_PLUGIN_VERSION,
        'python_packages':{name:version(name) for name in base_manifest['dependencies']['python_packages']}}
    entries = skill_manifest.get('skills')
    if not isinstance(entries,dict):
        raise ValueError('Invalid authoring skill manifest')
    selected = []
    for name, entry in sorted(entries.items()):
        identifier(name)
        if not isinstance(entry,dict) or type(entry.get('enabled',False)) is not bool:
            raise ValueError('Invalid authoring skill entry')
        channels = entry.get('channels') or ['all']
        if not isinstance(channels,list) or any(not isinstance(value,str) for value in channels):
            raise ValueError('Invalid authoring skill channels')
        if entry.get('enabled',False) and ('all' in channels or 'bank-runtime' in channels):
            selected.append(name)
    skill_root = root/('skills' if (root/'skills').exists() else 'skill')
    sources = [skill_root/name for name in selected]
    if any(parent.is_symlink() for source in sources for parent in (source,*source.parents)):
        raise ValueError('Linked authoring skill source')
    files, executable = {}, []
    with ExecutionConfiguration(config, None, selected, ()) as snapshot:
        directories = snapshot.skill_directories(sources)
        from agentscope.tool import Toolkit
        toolkit = Toolkit(skills_or_loaders=directories)
        if set(await toolkit._get_available_skills([])) != set(selected):
            raise ValueError('Authoring skill is not loadable')
        for directory in directories:
            directory = Path(directory)
            for path in sorted(directory.rglob('*')):
                if path.is_file():
                    relative = 'skills/'+directory.name+'/'+path.relative_to(directory).as_posix()
                    files[relative] = path.read_bytes()
                    if path.stat().st_mode & 0o111:
                        executable.append(relative)
        versions = snapshot.skill_versions()
        if (versions != [{'id':source.name,'hash':skill_directory_digest(source)} for source in sources]
                or config_raw != _read(root/'agent.json') or manifest_raw != _read(root/'skill.json')
                or (slot,model) != _model_policy(config)):
            raise ValueError('Authoring configuration changed during compilation')
    manifest = build_manifest(agent_id,policy,dependencies,files,executable)
    return {'node_id':node_id, 'agent_id':agent_id,'base_release_id':sha256(canonical(base_manifest)),
        'release_id':sha256(canonical(manifest)), 'package':{'agent_id':agent_id,'policy':policy,
        'dependencies':dependencies,'files':{path:base64.b64encode(content).decode('ascii') for path,content in files.items()},
        'executable':executable}}
