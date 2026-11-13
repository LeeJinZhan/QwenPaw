from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys
from unittest.mock import patch

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bank_runtime.sandbox.executor import RuntimeSandboxExecutor, SandboxExecutorError
from bank_runtime.sandbox.reading_results import materialize_reading_result


@pytest.mark.asyncio
@pytest.mark.parametrize('failure',['','denied','oversized','scope','expiry','path'])
async def test_reference_resolution_is_bounded_and_current(failure):
    from bank_runtime.sandbox.reading_results import resolve_reading_reference
    ref='ds1_'+'d'*64+'_'+'e'*64
    expiry=(datetime.now(timezone.utc)+timedelta(minutes=1)).isoformat()
    value=dict(document_ref=ref,expires_at=expiry,source=dict(file_id='file',sha256='a'*64,extension='.csv'))
    if failure=='scope':value['document_ref']='ds1_'+'f'*64+'_'+'0'*64
    if failure=='expiry':value['expires_at']=(datetime.now(timezone.utc)+timedelta(days=1)).isoformat()
    if failure=='path':value['source']['file_id']='../private'
    def handle(request):
        assert request.url.path.endswith('/resolve')
        assert json.loads(request.content)==dict(sandbox_context={'task_id':'task','expires_at':expiry},document_ref=ref)
        if failure=='denied':return httpx.Response(403)
        if failure=='oversized':return httpx.Response(200,content=b'x'*16385)
        return httpx.Response(200,json={'data':value})
    factory=httpx.AsyncClient
    executor=RuntimeSandboxExecutor('http://runtime','service',dict(task_id='task',expires_at=expiry))
    with patch('bank_runtime.sandbox.reading_results.httpx.AsyncClient',
               lambda **kwargs:factory(transport=httpx.MockTransport(handle),**kwargs)):
        if failure:
            with pytest.raises(SandboxExecutorError):await resolve_reading_reference(executor,ref)
        else:assert await resolve_reading_reference(executor,ref)==value


@pytest.mark.asyncio
@pytest.mark.parametrize('denied',[False,True])
async def test_reference_binding_retries_only_same_metadata_after_lost_ack(denied):
    from bank_runtime.sandbox.reading_results import bind_reading_reference
    calls = []
    ref = 'ds1_'+'d'*64+'_'+'e'*64
    def handle(request):
        calls.append(json.loads(request.content))
        if denied: return httpx.Response(403,json={'detail':'denied'})
        if len(calls)==1: raise httpx.ReadTimeout('Lost binding receipt')
        return httpx.Response(200,json={'data':{'document_ref':ref,'bound':True}})
    factory=httpx.AsyncClient
    executor=RuntimeSandboxExecutor('http://runtime','service',{'task_id':'task',
        'expires_at':(datetime.now(timezone.utc)+timedelta(minutes=1)).isoformat()})
    with patch('bank_runtime.sandbox.reading_results.httpx.AsyncClient',
               lambda **kwargs:factory(transport=httpx.MockTransport(handle),**kwargs)):
        if denied:
            with pytest.raises(SandboxExecutorError) as caught:
                await bind_reading_reference(executor,{'job_id':'a'*32,'result_ref':'b'*64},ref)
            assert caught.value.code=='FILE_ACCESS_DENIED'
            assert len(calls)==1
        else:
            await bind_reading_reference(executor,{'job_id':'a'*32,'result_ref':'b'*64},ref)
            assert len(calls)==2 and calls[0]==calls[1]


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['', 'corrupt', 'oversized', 'path', 'duplicate', 'denied'])
async def test_reading_result_is_bounded_verified_and_private(tmp_path, failure):
    task = tmp_path / 'task'
    task.mkdir()
    content = b'{"ready":true}'
    digest = hashlib.sha256(content).hexdigest()
    result = dict(job_id='c'*32, result_ref='d'*64)
    descriptor = dict(name='inventory.json', size_bytes=len(content), sha256=digest)
    descriptors = [descriptor]
    if failure == 'path': descriptor['name'] = '../outside'
    if failure == 'duplicate': descriptors = [descriptor,descriptor]
    requests = []
    def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        assert request.headers['authorization'] == 'Bearer service'
        assert body['sandbox_context']['task_id'] == 'task'
        if failure == 'denied': return httpx.Response(403)
        if 'name' not in body:
            return httpx.Response(200, json={'data':{**result,'files':descriptors}})
        payload = b'x'*len(content) if failure == 'corrupt' else content + b'x' if failure == 'oversized' else content
        return httpx.Response(200, content=payload, headers={
            'content-length':str(len(content)), 'x-content-sha256':digest})
    factory = httpx.AsyncClient
    def client(**kwargs): return factory(transport=httpx.MockTransport(handle), **kwargs)
    executor = RuntimeSandboxExecutor('http://runtime', 'service', {'task_id':'task',
        'expires_at':(datetime.now(timezone.utc)+timedelta(minutes=1)).isoformat()})
    with patch('bank_runtime.sandbox.reading_results.httpx.AsyncClient', client):
        if failure:
            with pytest.raises(SandboxExecutorError):
                await materialize_reading_result(executor, result, task)
            assert list(task.iterdir()) == []
            assert not (tmp_path/'outside').exists()
        else:
            work = await materialize_reading_result(executor, result, task)
            assert (work/'inventory.json').read_bytes() == content
            assert (work/'inventory.json').stat().st_mode & 0o777 == 0o600
            assert work.stat().st_mode & 0o777 == 0o700
            assert len(requests) == 2
