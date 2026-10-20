import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from bank_runtime.sandbox.executor import RuntimeSandboxExecutor, SandboxExecutorError
from bank_runtime.gateway.middleware import _sandbox_tool_response


def test_shell_recovery_feedback_retains_only_public_technical_fields():
    response=_sandbox_tool_response('call','execute_shell_command',{
        'exit_code':137,'container_available':False,'oom_killed':True,'state_lost':True,
        'notice':'sandbox_private_state_lost','execution_token':'secret','private_internal':'secret'})
    payload=json.loads(response.content[0].text)
    assert payload['container_available'] is False
    assert payload['oom_killed'] is True
    assert payload['state_lost'] is True
    assert payload['notice']=='sandbox_private_state_lost'
    assert 'secret' not in response.content[0].text


@pytest.mark.asyncio
@pytest.mark.parametrize('code,reason', [('WORKER_TIMEOUT','sandbox_command_timeout'),
    ('WORKER_UNAVAILABLE','sandbox_container_oom'),('FORBIDDEN','path_escape'),
    ('WORKER_TIMEOUT','sandbox_task_deadline_exhausted'),('FORBIDDEN','sandbox_task_authority_revoked')])
async def test_http_rejection_preserves_stable_technical_code_and_safe_reason(monkeypatch,code,reason):
    import bank_runtime.sandbox.executor as module
    class Response:
        status_code=503
        def json(self):return {'detail':{'code':code,'message':'host secret','details':{
            'reason':reason,'container_available':False,'oom_killed':True,'state_lost':True,
            'notice':'sandbox_private_state_lost','execution_token':'secret'}}}
    class Client:
        def __init__(self,**_):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*_):pass
        async def post(self,*_,**__):return Response()
    monkeypatch.setattr(module.httpx,'AsyncClient',Client)
    executor=RuntimeSandboxExecutor('http://runtime','token',{'expires_at':(datetime.now(timezone.utc)+timedelta(seconds=20)).isoformat()})
    with pytest.raises(SandboxExecutorError) as error:
        await executor.execute(tool_call_id='call',tool_name='read_file',tool_input={'file_path':'input/file'})
    assert error.value.code==code
    assert error.value.reason==reason
    assert error.value.details['state_lost'] is True
    assert 'secret' not in str(error.value.details)
    assert 'secret' not in str(error.value)
