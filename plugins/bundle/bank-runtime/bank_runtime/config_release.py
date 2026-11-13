"""Verified disposable caches for Runtime-owned immutable configuration releases."""
import base64
import copy
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
import json
import os
from pathlib import Path
import stat
import tempfile

from .release import BANK_RUNTIME_PLUGIN_VERSION
from .release_manifest import (DIGEST, MAX_BYTES, MAX_MANIFEST_BYTES, canonical, identifier,
    sha256, validate_manifest, validate_resources)


@dataclass(frozen=True)
class LoadedRelease:
    release_id: str
    manifest: dict
    config: object
    provider: object
    skill_directories: tuple[str, ...]


def check_dependencies(dependencies):
    if (dependencies['worker_build'] != os.environ.get('QWENPAW_BUILD_DIGEST')
            or dependencies['plugin_version'] != BANK_RUNTIME_PLUGIN_VERSION):
        raise ValueError('Configuration dependency build mismatch')
    for name, expected in dependencies['python_packages'].items():
        try:
            actual = version(name)
        except PackageNotFoundError:
            raise ValueError('Configuration dependency unavailable') from None
        if actual != expected:
            raise ValueError('Configuration dependency version mismatch')


def compile_policy(agent_id, policy):
    from qwenpaw.config.config import AgentProfileConfig
    from qwenpaw.providers import ProviderManager
    from qwenpaw.providers.provider import ModelInfo
    config = AgentProfileConfig(id=agent_id, name=agent_id, system_prompt_files=[], **policy['agent'])
    provider = copy.deepcopy(ProviderManager.get_instance().get_provider(config.active_model.provider_id))
    if provider is None:
        raise ValueError('Configuration provider reference unavailable')
    info = ModelInfo.model_validate(policy['model']['model_info'])
    provider.generate_kwargs = copy.deepcopy(policy['model']['generate_kwargs'])
    provider.models, provider.extra_models = [info], []
    from .context_budget import configured_budget
    configured_budget(info, provider.generate_kwargs)
    return config, provider


class ConfigReleaseCache:
    def __init__(self, directory):
        self.directory = Path(directory)

    def _root(self, agent_id, release_id):
        identifier(agent_id)
        if not isinstance(release_id, str) or not DIGEST.fullmatch(release_id):
            raise ValueError('Invalid configuration release ID')
        return self.directory / agent_id / release_id

    @staticmethod
    def _read(root, relative, limit):
        path = root / relative
        for parent in (path, *path.parents):
            if parent.is_symlink():
                raise ValueError('Symlinked configuration cache')
            if parent == root:
                break
        if not stat.S_ISREG(path.stat().st_mode):
            raise ValueError('Invalid configuration cache resource')
        with path.open('rb') as stream:
            result = stream.read(limit+1)
        if len(result) > limit:
            raise ValueError('Configuration cache byte limit')
        return result

    def load(self, agent_id, release_id):
        root = self._root(agent_id, release_id)
        content = self._read(root, 'manifest.json', MAX_MANIFEST_BYTES)
        if sha256(content) != release_id:
            raise ValueError('Configuration cache manifest mismatch')
        manifest = validate_manifest(json.loads(content))
        if manifest['agent_id'] != agent_id:
            raise ValueError('Configuration cache agent mismatch')
        check_dependencies(manifest['dependencies'])
        files = {entry['path']: self._read(root, entry['path'], entry['size_bytes']) for entry in manifest['files']}
        validate_resources(manifest, files)
        config, provider = compile_policy(agent_id, manifest['policy'])
        skills = sorted({entry['path'].split('/')[1] for entry in manifest['files']})
        return LoadedRelease(release_id, manifest, config, provider, tuple(str(root/'skills'/name) for name in skills))

    async def install(self, agent_id, payload):
        identifier(os.environ.get('QWENPAW_NODE_ID'))
        if not isinstance(payload, dict) or set(payload) != {'release_id', 'manifest', 'resources'}:
            raise ValueError('Invalid configuration transfer')
        manifest = validate_manifest(payload['manifest'])
        release_id = payload['release_id']
        root = self._root(agent_id, release_id)
        if manifest['agent_id'] != agent_id or sha256(canonical(manifest)) != release_id:
            raise ValueError('Configuration transfer identity mismatch')
        resources = payload['resources']
        expected = {entry['sha256'] for entry in manifest['files']}
        if (not isinstance(resources, dict) or set(resources) != expected
                or any(not isinstance(value, str) for value in resources.values())
                or sum(len(value) for value in resources.values()) > ((MAX_BYTES+2)//3)*4+4*len(expected)):
            raise ValueError('Configuration transfer resource limit')
        decoded = {key: base64.b64decode(value, validate=True) for key, value in resources.items()}
        files = {entry['path']: decoded[entry['sha256']] for entry in manifest['files']}
        validate_resources(manifest, files)
        check_dependencies(manifest['dependencies'])
        compile_policy(agent_id, manifest['policy'])
        root.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.prepare-', dir=root.parent) as staging:
            temporary = Path(staging)
            for entry in manifest['files']:
                target = temporary / entry['path']
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(files[entry['path']])
                target.chmod(0o500 if entry['executable'] else 0o400)
            skills = sorted({entry['path'].split('/')[1] for entry in manifest['files']})
            from agentscope.tool import Toolkit
            toolkit = Toolkit(skills_or_loaders=[str(temporary/'skills'/name) for name in skills])
            available = await toolkit._get_available_skills([])
            if set(available) != set(skills):
                raise ValueError('Configuration skill failed native loading')
            (temporary/'manifest.json').write_bytes(canonical(manifest))
            (temporary/'manifest.json').chmod(0o400)
            try:
                temporary.rename(root)
            except OSError:
                if not root.is_dir():
                    raise
                # Concurrent identical installation is idempotent only after a
                # complete readback. Never overwrite an existing corrupt cache.
                self.load(agent_id, release_id)
        return self.status(agent_id, release_id)

    def status(self, agent_id, release_id):
        loaded = self.load(agent_id, release_id)
        return {'agent_id': agent_id, 'release_id': release_id, 'loaded': True,
            'node_id': identifier(os.environ.get('QWENPAW_NODE_ID')),
            'worker_build': loaded.manifest['dependencies']['worker_build'],
            'plugin_version': BANK_RUNTIME_PLUGIN_VERSION,
            'skill_ids': [Path(path).name for path in loaded.skill_directories]}


def configured_cache():
    from qwenpaw.constant import WORKING_DIR
    return ConfigReleaseCache(os.environ.get('QWENPAW_CONFIG_CACHE_DIR') or Path(WORKING_DIR)/'bank-config-cache')


def build_config_release_router():
    from fastapi import APIRouter, Header, HTTPException, Request
    from .auth import require_service_identity
    from .content_review import require_review_agent
    from .production_guard import require_production_readiness
    router = APIRouter()

    def authorize(agent_id, authorization, header_agent_id):
        if require_service_identity(authorization, header_agent_id) != agent_id:
            raise HTTPException(401, 'Agent identity mismatch')
        require_production_readiness()
        require_review_agent(agent_id)

    @router.post('/agents/{agent_id}/config-releases/load')
    async def install(agent_id: str, request: Request, authorization: str | None = Header(default=None),
                      header_agent_id: str | None = Header(default=None, alias='X-Agent-Id')):
        authorize(agent_id, authorization, header_agent_id)
        try:
            content = bytearray()
            async for chunk in request.stream():
                content.extend(chunk)
                if len(content) > MAX_BYTES*4//3 + MAX_MANIFEST_BYTES*2:
                    raise ValueError('Configuration transfer size limit')
            return await configured_cache().install(agent_id, json.loads(content))
        except Exception:
            raise HTTPException(422, {'code': 'WORKER_RELEASE_LOAD_FAILED'}) from None

    @router.get('/agents/{agent_id}/config-releases/{release_id}')
    async def status(agent_id: str, release_id: str, authorization: str | None = Header(default=None),
                     header_agent_id: str | None = Header(default=None, alias='X-Agent-Id')):
        authorize(agent_id, authorization, header_agent_id)
        try:
            return configured_cache().status(agent_id, release_id)
        except Exception:
            raise HTTPException(503, {'code': 'WORKER_RELEASE_UNAVAILABLE'}) from None

    @router.post('/agents/{agent_id}/config-releases/compile')
    async def compile_current(agent_id: str, request: Request, authorization: str | None = Header(default=None),
                              header_agent_id: str | None = Header(default=None, alias='X-Agent-Id')):
        authorize(agent_id, authorization, header_agent_id)
        try:
            content = bytearray()
            async for chunk in request.stream():
                content.extend(chunk)
                if len(content) > MAX_MANIFEST_BYTES+4096:
                    raise ValueError('Compilation request exceeds limit')
            payload = json.loads(content)
            if not isinstance(payload,dict) or set(payload) != {'node_id','base_manifest'}:
                raise ValueError('Invalid compilation request')
            from .configuration_compile import compile_authoring_configuration
            # Bounded file copying and hashing must not stall chat/stop ingress.
            import asyncio
            return await asyncio.to_thread(lambda: asyncio.run(compile_authoring_configuration(agent_id,**payload)))
        except Exception:
            raise HTTPException(422, {'code':'WORKER_RELEASE_COMPILE_FAILED'}) from None
    return router
