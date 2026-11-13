from datetime import datetime,timedelta,timezone
import hashlib
import json
from pathlib import Path
import sys
from unittest.mock import patch
import httpx
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from bank_runtime.sandbox import cache as cache_module
from bank_runtime.sandbox.cache import TaskAttachmentCache
from bank_runtime.sandbox.file_refs import FileRefRegistry,FileRefError
from bank_runtime.sandbox.executor import RuntimeSandboxExecutor,AuthorizedReadingExecution,SandboxExecutorError
from bank_runtime.sandbox.broker import SandboxBrokerError,RuntimeFileBroker
from bank_runtime.sandbox.scope import SandboxRequestScope
from bank_runtime.sandbox.shared_files import restore,issue


@pytest.mark.asyncio
@pytest.mark.parametrize('failure',['','revoked','corrupt','size','metadata','expired'])
async def test_fresh_worker_restores_reference_with_existing_cache_limits_and_no_raw_grant(tmp_path,failure):
    content=b'amount\n10\n20\n'
    digest=hashlib.sha256(content).hexdigest()
    ref='fr2_66_'+digest+'_'+'e'*64
    expiry=(datetime.now(timezone.utc)+timedelta(minutes=2)).isoformat()
    context=dict(task_id='task',expires_at=expiry,shared_file_references=True)
    descriptor=dict(file_ref=ref,file_id='f',content_hash=digest,size_bytes=len(content),extension='.csv',
        content_type='text/csv',original_name='original.csv',expires_at=expiry)
    calls=[]
    def handle(request):
        path=request.url.path
        calls.append(path)
        assert request.headers['Authorization']=='Bearer service'
        assert json.loads(request.content)['sandbox_context']==context
        if failure=='revoked':return httpx.Response(403)
        if path.endswith('/batch-authorize'):
            return httpx.Response(200,json={'data':{'authorized':[],'denied':[{'file_id':'f','reason_code':'FILE_NOT_IN_SANDBOX'}]}})
        if path.endswith('/validate-materialized'):
            return httpx.Response(200,json={'data':{'files':[{k:descriptor[k] for k in ('file_id','content_hash','size_bytes')}]}})
        if path.endswith('/issue'):return httpx.Response(200,json={'data':{'files':[dict(file_id='f',file_ref=ref)]}})
        if path.endswith('/resolve'):
            value=dict(descriptor)
            if failure=='metadata':value['file_id']='another'
            if failure=='expired':value['expires_at']=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
            return httpx.Response(200,json={'data':value})
        assert path.endswith('/read')
        body=b'x'*len(content) if failure=='corrupt' else content
        return httpx.Response(200,content=body,headers={'X-Content-SHA256':digest,
            'Content-Length':str(len(content)+1 if failure=='size' else len(content))})
    async_factory,sync_factory=httpx.AsyncClient,httpx.Client
    cache=TaskAttachmentCache(root=tmp_path,max_files=2,max_total_bytes=100)
    registry=FileRefRegistry(root=tmp_path)
    with patch.object(cache_module,'_SHARED_CACHE',cache),patch('httpx.AsyncClient',
            lambda **kw:async_factory(transport=httpx.MockTransport(handle),**kw)),patch('httpx.Client',
            lambda **kw:sync_factory(transport=httpx.MockTransport(handle),**kw)):
        executor=RuntimeSandboxExecutor('http://runtime','service',context)
        try:
            if failure:
                with pytest.raises((SandboxBrokerError,httpx.HTTPError)):
                    await restore(executor,ref,'f',registry)
                assert not (tmp_path/'task/f.csv').exists()
            else:
                source=await restore(executor,ref,'f',registry)
                assert source.path.read_bytes()==content
                assert registry.reference_for_file('f',expected_task_id='task')==ref
                assert source.sha256==digest
                with pytest.raises(FileRefError):registry.resolve(ref,expected_task_id='other')
                registry.revoke_task('task')
                with pytest.raises(FileRefError):registry.resolve(ref)
        finally:await cache.cleanup('task')


@pytest.mark.asyncio
async def test_restoration_must_match_current_approved_parse(tmp_path):
    context=dict(task_id='task',shared_file_references=True)
    execution=AuthorizedReadingExecution(RuntimeSandboxExecutor('http://runtime','service',context),'call',
        'parse_documents',{'documents':[{'file_id':'f','file_ref':'approved'}]})
    with patch('bank_runtime.sandbox.shared_files.restore',side_effect=AssertionError('unapproved download')):
        with pytest.raises(SandboxExecutorError):await execution.restore_file_reference('other','f',FileRefRegistry(root=tmp_path))


@pytest.mark.asyncio
async def test_selection_failure_is_not_reinterpreted_as_cache_recovery():
    broker=RuntimeFileBroker('http://runtime','service')
    denied={'authorized':[],'denied':[dict(file_id='f',reason_code='FILE_NOT_IN_SANDBOX')]}
    scope=SandboxRequestScope('task',dict(shared_file_references=True),(),())
    with patch.object(broker,'_post',return_value=denied) as post:
        assert await broker.authorize_files(scope,['f'],selection_records=[{'file_id':'f'}])==denied
        assert post.call_count==1


@pytest.mark.asyncio
async def test_oversized_metadata_is_not_buffered():
    factory=httpx.AsyncClient
    broker=RuntimeFileBroker('http://runtime','service')
    context=dict(expires_at=(datetime.now(timezone.utc)+timedelta(minutes=1)).isoformat())
    with patch('httpx.AsyncClient',lambda **kw:factory(transport=httpx.MockTransport(
            lambda _:httpx.Response(200,content=b'x'*65537)),**kw)):
        with pytest.raises(SandboxBrokerError):await issue(broker,context,[])


def test_shared_reference_is_redacted_from_cross_task_session_text():
    from bank_runtime.session import _RUNTIME_REFERENCE
    token='fr2_66_'+'a'*64+'_'+'b'*64
    assert _RUNTIME_REFERENCE.sub('[redacted]','source '+token)=='source [redacted]'
