"""Fetch internal reading outputs into private ephemeral staging, with bounded IO."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

import httpx

_ENDPOINT = '/runtime/internal/sandbox/reading-results/read'
_MAX_BYTES = 8 * 1024**3
_MAX_MANIFEST = 2 * 1024**2


async def resolve_reading_reference(executor, document_ref):
    from .executor import SandboxExecutorError, _remaining_seconds
    from datetime import datetime, timezone
    if not isinstance(document_ref,str) or not re.fullmatch(r'ds1_[a-f0-9]{64}_[a-f0-9]{64}',document_ref):
        raise SandboxExecutorError('Invalid document reference','FILE_ACCESS_DENIED')
    remaining=_remaining_seconds(executor.sandbox_context)
    try:
        async with asyncio.timeout(remaining),httpx.AsyncClient(trust_env=False,follow_redirects=False,
                timeout=httpx.Timeout(min(30,remaining),connect=min(5,remaining))) as client:
            async with client.stream('POST',executor.base_url+'/runtime/internal/sandbox/reading-results/resolve',
                    json=dict(sandbox_context=executor.sandbox_context,document_ref=document_ref),
                    headers={'Authorization':f'Bearer {executor.token}'}) as response:
                if response.status_code in {401,403,404}:
                    raise SandboxExecutorError('Document reference unavailable','FILE_ACCESS_DENIED')
                if response.status_code>=400:raise ValueError('Reading reference unavailable')
                content=bytearray()
                async for block in response.aiter_bytes(8192):
                    if len(content)+len(block)>16384:raise ValueError('Reference response too large')
                    content.extend(block)
        value=json.loads(content)['data']
        if (not isinstance(value,dict) or set(value)!={'document_ref','source','expires_at'}
                or value['document_ref']!=document_ref):raise ValueError('Invalid reference receipt')
        source=value['source']
        if (not isinstance(source,dict) or set(source)!={'file_id','sha256','extension'}
                or not isinstance(source['file_id'],str) or not re.fullmatch('[A-Za-z0-9_-]{1,160}',source['file_id'])
                or not isinstance(source['sha256'],str) or not re.fullmatch('[a-f0-9]{64}',source['sha256'])
                or source['extension'] not in {'.csv','.tsv','.xlsx','.xls'}):
            raise ValueError('Invalid source receipt')
        expiry=datetime.fromisoformat(value['expires_at'].replace('Z','+00:00'))
        deadline=datetime.fromisoformat(executor.sandbox_context['expires_at'].replace('Z','+00:00'))
        if expiry.tzinfo is None or not datetime.now(timezone.utc)<expiry<=deadline:
            raise ValueError('Invalid reference expiry')
        return value
    except (ValueError,KeyError,TypeError,AttributeError,httpx.HTTPError,TimeoutError) as exc:
        raise SandboxExecutorError('Reading reference restoration failed','DOCUMENT_ENGINE_UNAVAILABLE') from exc


async def bind_reading_reference(executor, result, document_ref):
    from .executor import SandboxExecutorError, _remaining_seconds
    body = dict(sandbox_context=executor.sandbox_context, job_id=result['job_id'],
                result_ref=result['result_ref'], document_ref=document_ref)
    remaining = _remaining_seconds(executor.sandbox_context)
    try:
        async with asyncio.timeout(remaining), httpx.AsyncClient(trust_env=False,follow_redirects=False,
                timeout=httpx.Timeout(min(30,remaining),connect=min(5,remaining))) as client:
            # Repeated bindings are immutable/idempotent; a lost ACK never repeats parsing.
            for attempt in range(3):
                try:
                    async with client.stream('POST',executor.base_url+'/runtime/internal/sandbox/reading-results/bind',
                            json=body,headers={'Authorization':f'Bearer {executor.token}'}) as response:
                        content = bytearray()
                        async for block in response.aiter_bytes(8192):
                            if len(content)+len(block)>16384: raise ValueError('Binding response too large')
                            content.extend(block)
                        if response.status_code >= 500 and attempt < 2: continue
                        if response.status_code in {401,403}:
                            raise SandboxExecutorError('Reading reference unavailable','FILE_ACCESS_DENIED')
                        if response.status_code >= 400: raise ValueError('Binding rejected')
                        data = json.loads(content)['data']
                        if data != {'document_ref':document_ref,'bound':True}:
                            raise ValueError('Invalid binding receipt')
                        return
                except httpx.HTTPError:
                    if attempt == 2: raise
    except (ValueError,KeyError,TypeError,httpx.HTTPError,TimeoutError) as exc:
        raise SandboxExecutorError('Reading reference binding failed','DOCUMENT_ENGINE_UNAVAILABLE') from exc


async def materialize_reading_result(executor, result, task_directory):
    from .executor import SandboxExecutorError, _remaining_seconds
    job_id, result_ref = result.get('job_id'), result.get('result_ref')
    if (not isinstance(job_id, str) or not re.fullmatch('[a-f0-9]{32}', job_id)
            or not isinstance(result_ref, str) or not re.fullmatch('[a-f0-9]{64}', result_ref)):
        raise SandboxExecutorError('Invalid reading result', 'FILE_ACCESS_DENIED')
    task = Path(task_directory)
    if (task.name != executor.sandbox_context.get('task_id') or not task.is_dir()
            or any(path.is_symlink() for path in (task, *task.parents))):
        raise SandboxExecutorError('Invalid reading scope', 'FILE_ACCESS_DENIED')
    body = dict(sandbox_context=executor.sandbox_context, job_id=job_id, result_ref=result_ref)
    headers = {'Authorization':f'Bearer {executor.token}'}
    staging = None
    try:
        remaining = _remaining_seconds(executor.sandbox_context)
        async with asyncio.timeout(remaining), httpx.AsyncClient(
                trust_env=False, follow_redirects=False,
                timeout=httpx.Timeout(remaining, connect=min(5, remaining))) as client:
            async with client.stream('POST', executor.base_url + _ENDPOINT, json=body, headers=headers) as response:
                if response.status_code >= 400:
                    # Errors are also bounded; no unbounded aread() of an upstream body.
                    raise SandboxExecutorError('Reading result unavailable', 'FILE_ACCESS_DENIED')
                content = bytearray()
                async for block in response.aiter_bytes(65536):
                    if len(content) + len(block) > _MAX_MANIFEST:
                        raise ValueError('Reading manifest limit exceeded')
                    content.extend(block)
            manifest = json.loads(content)['data']
            if (not isinstance(manifest, dict) or set(manifest) != {'job_id','result_ref','files'}
                    or manifest['job_id'] != job_id or manifest['result_ref'] != result_ref
                    or not isinstance(manifest['files'], list) or len(manifest['files']) > 10000):
                raise ValueError('Invalid reading manifest')
            total, seen = 0, set()
            for item in manifest['files']:
                if (not isinstance(item, dict) or set(item) != {'name','size_bytes','sha256'}
                        or not _valid_name(item['name']) or item['name'] in seen
                        or type(item['size_bytes']) is not int or item['size_bytes'] < 0
                        or not isinstance(item['sha256'], str) or not re.fullmatch('[a-f0-9]{64}', item['sha256'])):
                    raise ValueError('Invalid reading file')
                seen.add(item['name'])
                total += item['size_bytes']
                if total > _MAX_BYTES:
                    raise ValueError('Reading result limit exceeded')
            if shutil.disk_usage(task).free < total + 128 * 1024**2:
                raise ValueError('Insufficient reading staging space')
            staging = Path(tempfile.mkdtemp(prefix='.reading-import-', dir=task))
            for item in manifest['files']:
                digest, size = hashlib.sha256(), 0
                async with client.stream('POST', executor.base_url + _ENDPOINT,
                        json={**body,'name':item['name']}, headers=headers) as response:
                    if response.status_code >= 400:
                        raise SandboxExecutorError('Reading result unavailable', 'FILE_ACCESS_DENIED')
                    if (response.headers.get('x-content-sha256') != item['sha256']
                            or response.headers.get('content-length') != str(item['size_bytes'])):
                        raise ValueError('Reading descriptor changed')
                    with (staging / item['name']).open('xb') as writer:
                        os.chmod(writer.name, 0o600)
                        async for block in response.aiter_bytes(1024 * 1024):
                            size += len(block)
                            if size > item['size_bytes']:
                                raise ValueError('Reading output exceeds bound')
                            digest.update(block)
                            writer.write(block)
                        writer.flush()
                        os.fsync(writer.fileno())
                if size != item['size_bytes'] or digest.hexdigest() != item['sha256']:
                    raise ValueError('Reading output integrity mismatch')
            _remaining_seconds(executor.sandbox_context)
            return staging
    except BaseException as exc:
        if staging is not None:
            shutil.rmtree(staging)
        if isinstance(exc, (ValueError, KeyError, TypeError, OSError, httpx.HTTPError, TimeoutError)):
            raise SandboxExecutorError('Reading result transfer failed', 'DOCUMENT_ENGINE_UNAVAILABLE') from exc
        raise


def _valid_name(name):
    return (isinstance(name, str) and 0 < len(name.encode('utf-8')) <= 255 and name not in {'.','..'}
            and not any(char in name for char in '/\\\x00:') and not any(ord(char) < 32 for char in name))
