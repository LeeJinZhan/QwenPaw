import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import httpx
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from bank_runtime.sandbox.executor import RuntimeSandboxExecutor, SandboxExecutorError
from bank_runtime.gateway.middleware import BankRuntimeGatewayMiddleware

@pytest.mark.asyncio
async def test_accepted_submit_and_query_response_loss_resolves_same_call_once(monkeypatch):
    import bank_runtime.sandbox.executor as module
    original=httpx.AsyncClient
    posts=[]
    state={'accepted':False,'query_lost':False,'executions':0}
    async def handler(request):
        payload=json.loads(request.content)
        posts.append((request.url.path,payload,request.extensions['timeout']))
        if request.url.path.endswith('/submit'):
            state['executions']+=1
            state['accepted']=True
            raise httpx.ReadError('lost accepted response',request=request)
        if request.url.path.endswith('/query'):
            assert state['accepted']
            if not state['query_lost']:
                state['query_lost']=True
                raise httpx.ReadError('lost query response',request=request)
            return httpx.Response(200,json={'data':{'status':'succeeded','result':{'exit_code':0,'stdout':'actual','stderr':''}}})
        pytest.fail('native client used old synchronous endpoint')
    monkeypatch.setattr(module.httpx,'AsyncClient',lambda **kwargs:original(transport=httpx.MockTransport(handler),**kwargs))
    context={'native_analysis_enabled':True,'isolation_level':'container','expires_at':(datetime.now(timezone.utc)+timedelta(seconds=10)).isoformat()}
    result=await RuntimeSandboxExecutor('http://runtime','secret',context).execute(tool_call_id='original',tool_name='execute_shell_command',tool_input={'command':'printf actual','timeout':1})
    assert result['stdout']=='actual'
    assert state['executions']==1
    assert len(posts)==3
    for path,payload,timeout in posts:
        assert payload['tool_call_id']=='original'
        assert timeout['read'] <= 30 and timeout['connect'] <= 5
        if path.endswith('/query'):
            assert 'arguments' not in payload
            assert payload['input_hash'].startswith('sha256:')

@pytest.mark.asyncio
@pytest.mark.parametrize('exit_code',[0,7])
async def test_native_actual_result_survives_terminal_callback_response_loss(monkeypatch,exit_code):
    from types import SimpleNamespace
    from bank_runtime.gateway.middleware import BankRuntimeGatewayMiddleware
    reports=[]
    guards=[]
    class Client:
        async def report_guard(self,prepared,decision):guards.append(decision)
        async def report_result(self,call,status,duration,error):
            reports.append(status)
            if len(reports)==1:raise httpx.ReadError('accepted callback receipt lost')
    class Executor:
        sandbox_context={'native_analysis_enabled':True,'isolation_level':'container',
            'expires_at':(datetime.now(timezone.utc)+timedelta(seconds=10)).isoformat()}
        async def execute(self,**kwargs):return {'exit_code':exit_code,'stdout':'actual stdout','stderr':'Traceback: actual error' if exit_code else ''}
    middleware=BankRuntimeGatewayMiddleware(Client(),sandbox_executor=Executor(),native_analysis_enabled=True)
    raw={'command':'python scratch/run.py'}
    middleware.prepare('execute_shell_command',raw,{'tool_call_id':'original'})
    async def forbidden(**kwargs):
        raise AssertionError('host tool must not execute')
        yield
    call=SimpleNamespace(name='execute_shell_command',input=json.dumps(raw),id='native-response')
    items=[item async for item in middleware.on_acting(SimpleNamespace(),{'tool_call':call},forbidden)]
    assert guards==['allow']
    assert reports==['completed' if exit_code==0 else 'failed']*2
    assert len(items)==1
    result=json.loads(items[0].content[0].text)
    assert result['stdout']=='actual stdout'
    assert result['exit_code']==exit_code
    if exit_code:assert result['stderr']=='Traceback: actual error'

@pytest.mark.asyncio
@pytest.mark.parametrize('exit_code,cancelled',[(0,False),(7,False),(0,True)])
async def test_real_gateway_client_all_terminal_receipts_lost_never_reexecutes(monkeypatch,tmp_path,exit_code,cancelled):
    from types import SimpleNamespace
    from bank_runtime.gateway.client import GatewayClient, GatewayConfig
    from bank_runtime.gateway.outbox import GatewayResultOutbox
    import bank_runtime.sandbox.executor as module
    original=httpx.AsyncClient
    state={'guard':0,'exec':0,'submit_lost':False,'query_lost':False,'terminal':None,'audits':0}
    reports=[]
    async def handler(request):
        payload=json.loads(request.content)
        if request.url.path.endswith('/submit'):
            state['exec']+=1;state['submit_lost']=True
            raise httpx.ReadError('submit accepted',request=request)
        if request.url.path.endswith('/query'):
            if not state['query_lost']:
                state['query_lost']=True;raise httpx.ReadError('query lost',request=request)
            return httpx.Response(200,json={'data':{'status':'cancelled' if cancelled else ('failed' if exit_code else 'succeeded'),
                'result':{'exit_code':exit_code,'stdout':'actual stdout','stderr':'actual guest stderr'}}})
        if payload['phase']=='guard':
            state['guard']+=1
            return httpx.Response(200,json={'tool_call_id':'original','guard_decision':'allow','status':'executing'})
        assert payload['phase']=='result'
        assert 'stdout' not in payload and 'stderr' not in payload and 'arguments' not in payload
        reports.append(payload['status'])
        if state['terminal'] is None:state['terminal']=payload['status'];state['audits']+=1
        raise httpx.ReadError('all accepted terminal receipts lost',request=request)
    monkeypatch.setattr(module.httpx,'AsyncClient',lambda **kwargs:original(transport=httpx.MockTransport(handler),**kwargs))
    config=GatewayConfig('http://runtime','/runtime/v1/tool-calls','secret','task','session','tool-session','policy','scope','hash','2','trace','assistant')
    client=GatewayClient(config,outbox=GatewayResultOutbox(tmp_path/'outbox'))
    context={'native_analysis_enabled':True,'isolation_level':'container','expires_at':(datetime.now(timezone.utc)+timedelta(seconds=10)).isoformat()}
    executor=RuntimeSandboxExecutor('http://runtime','secret',context)
    middleware=BankRuntimeGatewayMiddleware(client,sandbox_executor=executor,native_analysis_enabled=True)
    raw={'command':'python scratch/run.py'}
    middleware.prepare('execute_shell_command',raw,{'tool_call_id':'original','permit':{'payload':{'permit_id':'permit','permit_nonce':'nonce'}}})
    async def forbidden(**kwargs):
        raise AssertionError('host execution')
        yield
    call=SimpleNamespace(name='execute_shell_command',input=json.dumps(raw),id='response')
    items=[item async for item in middleware.on_acting(SimpleNamespace(),{'tool_call':call},forbidden)]
    expected='cancelled' if cancelled else ('failed' if exit_code else 'completed')
    assert state['exec']==state['guard']==state['audits']==1
    assert reports==[expected]*3 and state['terminal']==expected
    assert len(items)==1
    assert json.loads(items[0].content[0].text)['stdout']=='actual stdout'
    assert client.outbox.pending('task')[0]['status']==expected

@pytest.mark.asyncio
async def test_invalid_json_retry_uses_same_bounded_backoff(monkeypatch):
    import bank_runtime.sandbox.executor as module
    original=httpx.AsyncClient;requests=[];delays=[]
    async def handler(request):
        requests.append(request.url.path)
        if len(requests)<4:return httpx.Response(200,content=b'broken json')
        return httpx.Response(200,json={'data':{'status':'succeeded','result':{'exit_code':0}}})
    async def pause(delay):delays.append(delay)
    monkeypatch.setattr(module.httpx,'AsyncClient',lambda **kwargs:original(transport=httpx.MockTransport(handler),**kwargs))
    monkeypatch.setattr(module.asyncio,'sleep',pause)
    context={'native_analysis_enabled':True,'isolation_level':'container','expires_at':(datetime.now(timezone.utc)+timedelta(seconds=10)).isoformat()}
    await RuntimeSandboxExecutor('http://runtime','secret',context).execute(tool_call_id='original',tool_name='execute_shell_command',tool_input={'command':'true'})
    assert delays==[.05,.1,.2]
    assert len([path for path in requests if path.endswith('/submit')])==1

@pytest.mark.asyncio
async def test_metadata_unavailable_keeps_same_call_poll_and_single_submit(monkeypatch):
    import bank_runtime.sandbox.executor as module
    original=httpx.AsyncClient;requests=[]
    async def handler(request):
        payload=json.loads(request.content);requests.append((request.url.path,payload))
        if len(requests)<3:return httpx.Response(200,json={'data':{'status':'unavailable','reason':'sandbox_job_metadata_busy','job_id':'job'}})
        return httpx.Response(200,json={'data':{'status':'succeeded','result':{'exit_code':0,'stdout':'actual'}}})
    monkeypatch.setattr(module.httpx,'AsyncClient',lambda **kwargs:original(transport=httpx.MockTransport(handler),**kwargs))
    context={'native_analysis_enabled':True,'isolation_level':'container','expires_at':(datetime.now(timezone.utc)+timedelta(seconds=10)).isoformat()}
    result=await RuntimeSandboxExecutor('http://runtime','secret',context).execute(tool_call_id='original',tool_name='execute_shell_command',tool_input={'command':'true'})
    assert result['stdout']=='actual'
    assert len([path for path,payload in requests if path.endswith('/submit')])==1
    assert all(payload['tool_call_id']=='original' for path,payload in requests)
