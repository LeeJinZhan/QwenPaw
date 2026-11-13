"""Portable bank-worker-config-v1 contract; no local paths or credentials."""
import hashlib
import json
import posixpath
import re
from urllib.parse import unquote, urlsplit

FORMAT = 'bank-worker-config-v1'
MAX_FILES = 2048
MAX_BYTES = 64 * 1024 * 1024
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
IDENTIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z')
DIGEST = re.compile(r'[a-f0-9]{64}\Z')


class ReleaseConflict(ValueError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8')


def sha256(value):
    return hashlib.sha256(value).hexdigest()


def identifier(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value) or value in ('.', '..'):
        raise ValueError('Invalid release identifier')
    return value


def _no_credentials(value, depth=0):
    if depth > 32:
        raise ValueError('Configuration nesting limit')
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or re.sub(r'[^a-z0-9]', '', key.lower()) in {
                'apikey', 'secret', 'secretkey', 'clientsecret', 'password', 'authorization',
                'accesstoken', 'refreshtoken', 'credentials', 'cookie', 'headers', 'extraheaders', 'baseurl'}:
                raise ValueError('Configuration must reference credentials through provider_id')
            _no_credentials(item, depth+1)
    elif isinstance(value, list):
        for item in value:
            _no_credentials(item, depth+1)


def validate_policy(policy):
    if not isinstance(policy, dict) or set(policy) != {'agent', 'prompt', 'tool_names', 'model'}:
        raise ValueError('Invalid release policy')
    agent = policy['agent']
    if not isinstance(agent, dict) or set(agent) != {'language', 'approval_level', 'active_model', 'running'}:
        raise ValueError('Invalid public agent configuration')
    if agent['language'] not in ('zh', 'en') or agent['approval_level'] not in ('STRICT', 'SMART', 'AUTO'):
        raise ValueError('Invalid release language or approval policy')
    slot = agent['active_model']
    if not isinstance(slot, dict) or set(slot) != {'provider_id', 'model'}:
        raise ValueError('Invalid release model reference')
    identifier(slot['provider_id'])
    if not isinstance(slot['model'], str) or not 1 <= len(slot['model']) <= 256:
        raise ValueError('Invalid release model')
    if not isinstance(agent['running'], dict) or not isinstance(policy['prompt'], str) or len(policy['prompt']) > 131072:
        raise ValueError('Invalid release instructions')
    names = policy['tool_names']
    if not isinstance(names, list) or len(names) > 256 or any(not isinstance(name, str) for name in names):
        raise ValueError('Invalid release tools')
    if len(set(names)) != len(names):
        raise ValueError('Duplicate release tools')
    for name in names:
        identifier(name)
    model = policy['model']
    if not isinstance(model, dict) or set(model) != {'generate_kwargs', 'model_info'} or any(
            not isinstance(model[key], dict) for key in model):
        raise ValueError('Invalid model policy')
    if model['model_info'].get('id') != slot['model']:
        raise ValueError('Model policy identity mismatch')
    _no_credentials(policy)


def validate_dependencies(dependencies):
    if not isinstance(dependencies, dict) or set(dependencies) != {'worker_build', 'plugin_version', 'python_packages'}:
        raise ValueError('Invalid release dependencies')
    if not isinstance(dependencies['worker_build'], str) or not DIGEST.fullmatch(dependencies['worker_build']):
        raise ValueError('Release requires a pinned worker build')
    if not isinstance(dependencies['plugin_version'], str) or not 1 <= len(dependencies['plugin_version']) <= 64:
        raise ValueError('Invalid plugin version')
    packages = dependencies['python_packages']
    if not isinstance(packages, dict) or len(packages) > 256:
        raise ValueError('Invalid release packages')
    for name, version in packages.items():
        identifier(name)
        if not isinstance(version, str) or not re.fullmatch(r'[A-Za-z0-9_.+!-]{1,128}', version):
            raise ValueError('Release requires exact package versions')


def resource_path(path):
    if not isinstance(path, str) or len(path) > 512 or re.search(r'[\\\x00-\x1f<>:"|?*]', path):
        raise ValueError('Invalid release resource path')
    parts = path.split('/')
    if len(parts) < 3 or parts[0] != 'skills' or any(
            not part or part in ('.', '..') or part.endswith((' ', '.')) for part in parts):
        raise ValueError('Invalid release resource path')
    identifier(parts[1])
    if any(part.lower() in {'.git', '.env', '__pycache__'} for part in parts):
        raise ValueError('Private or generated files cannot be release resources')
    if any(re.fullmatch(r'(con|prn|aux|nul|com[1-9]|lpt[1-9])', part.split('.')[0], re.I) for part in parts):
        raise ValueError('Nonportable release resource path')
    return path


def validate_manifest(manifest):
    if not isinstance(manifest, dict) or set(manifest) != {'format', 'agent_id', 'policy', 'dependencies', 'files'}:
        raise ValueError('Invalid release manifest')
    if manifest['format'] != FORMAT:
        raise ValueError('Unsupported release format')
    identifier(manifest['agent_id'])
    validate_policy(manifest['policy'])
    validate_dependencies(manifest['dependencies'])
    entries = manifest['files']
    if not isinstance(entries, list) or len(entries) > MAX_FILES:
        raise ValueError('Release resource count limit')
    paths, folded, total = set(), set(), 0
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {'path', 'sha256', 'size_bytes', 'executable'}:
            raise ValueError('Invalid release resource descriptor')
        path = resource_path(entry['path'])
        if path.casefold() in folded:
            raise ValueError('Duplicate release resource path')
        paths.add(path)
        folded.add(path.casefold())
        if type(entry['size_bytes']) is not int or entry['size_bytes'] < 0 or type(entry['executable']) is not bool:
            raise ValueError('Invalid release resource metadata')
        total += entry['size_bytes']
        if total > MAX_BYTES or not isinstance(entry['sha256'], str) or not DIGEST.fullmatch(entry['sha256']):
            raise ValueError('Release resource integrity or byte limit')
    for path in paths:
        if '/'.join(path.split('/')[:2])+'/SKILL.md' not in paths:
            raise ValueError('Incomplete release skill')
        if any('/'.join(path.split('/')[:length]) in paths for length in range(3, len(path.split('/')))):
            raise ValueError('Release file and directory collision')
    if len(canonical(manifest)) > MAX_MANIFEST_BYTES:
        raise ValueError('Release manifest limit')
    return manifest


def validate_resources(manifest, files):
    validate_manifest(manifest)
    if not isinstance(files, dict) or set(files) != {entry['path'] for entry in manifest['files']}:
        raise ValueError('Release resource closure mismatch')
    for entry in manifest['files']:
        content = files[entry['path']]
        if not isinstance(content, bytes) or len(content) != entry['size_bytes'] or sha256(content) != entry['sha256']:
            raise ValueError('Release resource hash mismatch')
        if entry['path'].endswith('.md'):
            text = content.decode('utf-8')
            root = '/'.join(entry['path'].split('/')[:2])+'/'
            for link in re.findall(r'\]\(\s*(<[^>]+>|[^\s)]+)', text):
                parsed = urlsplit(link.strip('<>'))
                if parsed.scheme or parsed.netloc or not parsed.path:
                    continue
                path = posixpath.normpath(posixpath.join(posixpath.dirname(entry['path']), unquote(parsed.path)))
                if not path.startswith(root) or (path not in files and not any(key.startswith(path+'/') for key in files)):
                    raise ValueError('Missing or escaping release reference')


def build_manifest(agent_id, policy, dependencies, files, executable=()):
    executable = set(executable)
    if not isinstance(files, dict) or len(files) > MAX_FILES or not executable.issubset(files):
        raise ValueError('Invalid release resources')
    manifest = {'format': FORMAT, 'agent_id': agent_id, 'policy': policy, 'dependencies': dependencies,
        'files': [{'path': path, 'sha256': sha256(content), 'size_bytes': len(content),
                   'executable': path in executable} for path, content in sorted(files.items())]}
    validate_resources(manifest, files)
    return manifest
