"""Offline export of a committed bank Session; never follows arbitrary paths.

Run with the bank plugin on PYTHONPATH while writers are stopped. The bundle
contains user content and must be handled as a protected migration artifact.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path

from .remote_state import canonical_json, canonical_hash
from .session import _remote_agent_state

FORMAT = 'bank-session-migration-v1'
MAX_BYTES = 16 * 1024 * 1024


def export_session(session_file, *, result_root=None, result_files=(), result_aliases=()):
    source = Path(session_file)
    if source.is_symlink() or not source.is_file() or source.stat().st_size > MAX_BYTES:
        raise ValueError('Invalid session source')
    raw = _bounded_read(source,MAX_BYTES)
    saved = json.loads(raw)
    marker = saved.get('bank_runtime_scope')
    required = {'agent_id', 'user_id', 'channel', 'session_id', 'last_committed_task_id'}
    if (not isinstance(marker, dict) or set(marker) != required
            or marker.get('channel') != 'bank-runtime'
            or any(not isinstance(value, str) or not value or len(value) > 191 for value in marker.values())):
        raise ValueError('Uncommitted or unscoped session')
    state = _remote_agent_state(saved.get('agent'))
    if state.get('state', {}).get('session_id') != marker['session_id']:
        raise ValueError('Session identity mismatch')
    replacements, results, resources, aliases = {}, [], {}, {}
    total_bytes = len(raw)
    root = Path(result_root).resolve(strict=True) if result_root else None
    for requested in result_files:
        if len(resources) >= 1024:
            raise ValueError('Migration result count exceeds limit')
        path = Path(requested)
        resolved = path.resolve(strict=True)
        if root is None or not resolved.is_relative_to(root) or path.is_symlink() or not resolved.is_file():
            raise ValueError('Result file outside approved root')
        # Check every lexical parent: a symlink within the root is not authority.
        if any(parent.is_symlink() for parent in path.parents):
            raise ValueError('Result file link rejected')
        if resolved.stat().st_size > MAX_BYTES:
            raise ValueError('Result file exceeds limit')
        content = _bounded_read(resolved,MAX_BYTES).decode('utf-8')
        total_bytes += len(content.encode('utf-8'))
        if total_bytes > 64 * 1024 * 1024:
            raise ValueError('Migration bundle exceeds limit')
        resources[str(resolved)] = content
        aliases[str(path)] = str(resolved)
        aliases[str(resolved)] = str(resolved)

    # An archive may be mounted at a different path. Only an explicit reviewed
    # old-path -> selected-file + original-byte digest mapping grants an alias.
    if not isinstance(result_aliases,(list,tuple)) or len(result_aliases)>1024:
        raise ValueError('Invalid migration result mapping')
    import re
    for mapping in result_aliases:
        if not isinstance(mapping,dict) or set(mapping)!={'source','file','sha256'}:
            raise ValueError('Invalid migration result mapping')
        alias=mapping['source']
        if (not isinstance(alias,str) or not 1<=len(alias)<=2048
                or not (alias.startswith('/') or re.match(r'^[A-Za-z]:[/\\]',alias))
                or any(ord(c)<32 for c in alias) or not isinstance(mapping['file'],str)
                or not isinstance(mapping['sha256'],str) or not re.fullmatch('[a-f0-9]{64}',mapping['sha256'])):
            raise ValueError('Invalid migration result mapping')
        selected=str(Path(mapping['file']).resolve(strict=True))
        if (selected not in resources or hashlib.sha256(resources[selected].encode()).hexdigest()!=mapping['sha256']
                or alias in aliases and aliases[alias]!=selected):
            raise ValueError('Migration result mapping does not match selected bytes')
        aliases[alias]=selected

    # Build content-addressed references from leaves to parents. Hashing the
    # original parent bytes would preserve a node-local dependency inside OSS.
    import re
    patterns = {alias:re.compile(r'(?<![\w./\\-])'+re.escape(alias)+r'(?![\w./\\-])') for alias in aliases}
    resolved_refs, visiting, result_refs = {}, set(), set()
    def resolve_result(name):
        if name in resolved_refs:
            return resolved_refs[name]
        if name in visiting or len(visiting) >= 64:
            raise ValueError('Cyclic or excessively deep result references')
        visiting.add(name)
        content = resources[name]
        for alias in sorted(aliases,key=len,reverse=True):
            pattern = patterns[alias]
            if pattern.search(content):
                reference = resolve_result(aliases[alias])
                content = pattern.sub(lambda _:reference,content)
        if any(part in content.replace('\\','/') for part in ('/tool_results/','/toolresult/')):
            raise ValueError('Nested result must be included explicitly')
        if len(content.encode('utf-8')) > MAX_BYTES:
            raise ValueError('Migrated result exceeds limit')
        content_hash = hashlib.sha256(content.encode('utf-8')).hexdigest()
        reference = 'runtime-result:result_' + hashlib.sha256(('tool_result\0' + content_hash).encode()).hexdigest()
        if reference not in result_refs:
            results.append(dict(result_ref=reference,kind='tool_result',content=content,content_hash=content_hash))
            result_refs.add(reference)
        resolved_refs[name] = reference
        visiting.remove(name)
        return reference
    for alias,name in aliases.items():
        replacements[alias] = resolve_result(name)

    def migrate_notices(value):
        if isinstance(value, dict):
            if value.get('role')=='user':return
            truncation = value.get('qwenpaw_truncation')
            if isinstance(truncation,dict):
                from qwenpaw.agents.tools.utils import build_truncation_metadata
                for info in truncation.values():
                    if (isinstance(info,dict) and info.get('file_path')
                            and info['file_path'] not in replacements):
                        raise ValueError('Truncated result must be included explicitly')
                    if isinstance(info,dict) and info.get('file_path') in replacements:
                        old_notice = info.get('notice')
                        replacement = build_truncation_metadata(
                            **{key:info[key] for key in ('file_size_bytes','total_lines','start_line','max_bytes','excerpt_bytes','read_from')},
                            file_path=replacements[info['file_path']])['qwenpaw_truncation']['0']['notice']
                        if isinstance(old_notice,str) and old_notice:
                            replacements[old_notice] = replacement
                # The regular replacement traversal updates this metadata too.
            for child in value.values():
                migrate_notices(child)
        elif isinstance(value,list):
            for child in value:
                migrate_notices(child)

    migrate_notices(state)

    def replace(value):
        if isinstance(value, dict):
            if value.get('role') == 'user':
                return copy.deepcopy(value)  # Preserve user prose verbatim.
            return {key: replace(child) for key, child in value.items()}
        if isinstance(value, list):
            return [replace(child) for child in value]
        if isinstance(value, str):
            for path, reference in sorted(replacements.items(),key=lambda item:len(item[0]),reverse=True):
                value = (patterns[path].sub(lambda _:reference,value) if path in patterns
                         else value.replace(path,reference))
            if any(part in value.replace('\\', '/') for part in ('/tool_results/', '/toolresult/')):
                raise ValueError('Referenced tool result must be included explicitly')
        return value

    state = replace(state)
    bundle = dict(format=FORMAT, marker=marker, source_sha256=hashlib.sha256(raw).hexdigest(),
                  agent_state=state, results=results)
    bundle['bundle_hash'] = canonical_hash(bundle)
    if len(canonical_json(bundle).encode('utf-8')) > 64 * 1024 * 1024:
        raise ValueError('Migration bundle exceeds transport limit')
    return bundle


def _bounded_read(path, maximum):
    with path.open('rb') as stream:
        value=stream.read(maximum+1)
    if len(value)>maximum:raise ValueError('Migration input exceeds limit')
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--session-file', required=True, type=Path)
    parser.add_argument('--result-root', type=Path)
    parser.add_argument('--result-file', action='append', default=[], type=Path)
    parser.add_argument('--result-map', type=Path, help='Reviewed old-path/file/SHA256 JSON array; selected files only')
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    try:
        aliases=json.loads(_bounded_read(args.result_map,1024**2)) if args.result_map else ()
        bundle = export_session(args.session_file, result_root=args.result_root, result_files=args.result_file,
                                result_aliases=aliases)
        # Never overwrite a sealed source or an earlier export.
        with os.fdopen(os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w', encoding='utf-8') as stream:
            stream.write(canonical_json(bundle) + '\n')
        print(json.dumps({'status':'exported','bundle_hash':bundle['bundle_hash'],
            'source_sha256':bundle['source_sha256'], 'results':len(bundle['results']), 'source_files_deleted':0}))
        return 0
    except Exception as exc:
        print(json.dumps({'status':'blocked','error_type':type(exc).__name__,'source_files_deleted':0}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
