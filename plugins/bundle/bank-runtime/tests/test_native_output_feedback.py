"""Actual Linux producer bytes through the public native ToolResponse contract."""
import importlib.util
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from bank_runtime.gateway.middleware import _sandbox_tool_response, ToolResultState
from bank_runtime.sandbox.executor import _technical_rejection


def process_module():
    path=Path(__file__).resolve().parents[5]/'agentic-runtime/bank_agent_runtime/infrastructure/bounded_process.py'
    if sys.platform != 'linux' or not path.is_file():
        pytest.skip('Cross-repository Linux Runtime process transport fixture is unavailable')
    spec=importlib.util.spec_from_file_location('native_bounded_probe',path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('exit_code',[0,7])
def test_actual_dual_stream_excerpt_retains_raw_counts_and_real_exit(exit_code):
    module=process_module()
    run=module.bounded_run([sys.executable,'-c',
        "import os; os.write(1,b'x'*100000+b'OUT-END'); os.write(2,b'y'*100000+b'ERR-END'); raise SystemExit("+str(exit_code)+")"],
        timeout_seconds=5,excerpt_bytes=65536,max_output_bytes=16*1024**2,max_total_bytes=32*1024**2)
    response=_sandbox_tool_response('provider-new-id','execute_shell_command',{
        'exit_code':run.returncode,'stdout':run.stdout.decode(),'stderr':run.stderr.decode(),
        'stdout_bytes':run.stdout_bytes,'stderr_bytes':run.stderr_bytes,
        'stdout_truncated':run.stdout_truncated,'stderr_truncated':run.stderr_truncated,
        'technical_reason':run.technical_reason,'excerpt_mode':'head_tail',
        'execution_token':'secret','log_path':'private-path'})
    result=json.loads(response.content[0].text)
    assert result['stdout_bytes']==100007 and result['stderr_bytes']==100007
    assert result['stdout_truncated'] is True and result['stderr_truncated'] is True
    assert result['stdout'].endswith('OUT-END') and result['stderr'].endswith('ERR-END')
    assert result['exit_code']==exit_code
    assert result['technical_reason']=='' and result['excerpt_mode']=='head_tail'
    assert response.state==(ToolResultState.SUCCESS if exit_code==0 else ToolResultState.ERROR)
    assert response.id=='provider-new-id'
    assert 'secret' not in response.content[0].text and 'private-path' not in response.content[0].text


def test_technical_metadata_is_bounded_and_whitelisted():
    response=_sandbox_tool_response('id','execute_shell_command',{
        'exit_code':0,'stdout':'actual','stderr':'','technical_reason':'private/'+'x'*10000,
        'stdout_bytes':True,'stderr_bytes':10**5000,'excerpt_mode':'private-secret'})
    result=json.loads(response.content[0].text)
    assert result['technical_reason']==''
    assert result['stdout_bytes']==6 and result['stderr_bytes']==0
    assert result['excerpt_mode']=='complete'
    class Response:
        def json(self):return {'detail':{'code':'WORKER_FAILED','details':{
            'reason':'sandbox_rpc_frame_invalid','technical_reason':'sandbox_rpc_frame_invalid',
            'stdout_bytes':1024,'stderr_bytes':2,'stdout_truncated':True,'stderr_truncated':False,
            'private_secret':'hidden'}}}
    error=_technical_rejection(Response())
    assert error.reason=='sandbox_rpc_frame_invalid'
    assert error.details['stdout_bytes']==1024
    assert error.details['stdout_truncated'] is True
    assert 'hidden' not in str(error.details)


def test_actual_python_failure_diagnostics_then_explicit_new_call_correction():
    module=process_module()
    failing="import sys,traceback; print('x'*100000);\ntry: print(missing_model_variable)\nexcept NameError: traceback.print_exc(); raise SystemExit(7)"
    scripts=[('provider-first-call',failing),('provider-new-call',"import statistics; print('corrected-script', statistics.mean([2,4]))")]
    responses=[]
    for identifier,script in scripts:
        run=module.bounded_run([sys.executable,'-c',script],timeout_seconds=5,
            excerpt_bytes=65536,max_output_bytes=16*1024**2,max_total_bytes=32*1024**2)
        responses.append(_sandbox_tool_response(identifier,'execute_shell_command',{
            'exit_code':run.returncode,'stdout':run.stdout.decode(),'stderr':run.stderr.decode(),
            'stdout_bytes':run.stdout_bytes,'stderr_bytes':run.stderr_bytes,
            'stdout_truncated':run.stdout_truncated,'stderr_truncated':run.stderr_truncated,
            'technical_reason':run.technical_reason,'excerpt_mode':'head_tail' if run.stdout_truncated else 'complete'}))
    first,second=(json.loads(response.content[0].text) for response in responses)
    assert first['exit_code']==7 and 'NameError' in first['stderr']
    assert 'missing_model_variable' in first['stderr']
    assert first['stdout_truncated'] is True
    assert responses[0].state==ToolResultState.ERROR
    assert second['exit_code']==0 and second['stdout'].strip()=='corrected-script 3'
    assert responses[1].state==ToolResultState.SUCCESS
    assert responses[0].id!=responses[1].id


@pytest.mark.parametrize('complete,reason',[(True,''),(False,'sandbox_log_metadata_failed')])
def test_trusted_log_coordinates_and_saved_counts_reach_actual_model_response(complete,reason):
    root='/workspace/public/execution_logs/sandbox_run_actual/'
    response=_sandbox_tool_response('new-call','execute_shell_command',{'exit_code':0,'stdout':'excerpt','stderr':'',
        'stdout_bytes':100000,'stderr_bytes':0,'stdout_log_path':root+'stdout.log','stderr_log_path':root+'stderr.log',
        'stdout_log_bytes':100000,'stderr_log_bytes':0,'logs_complete':complete,'log_interruption':reason,
        'technical_reason':reason,'host_root':'/private-host-root','execution_token':'secret'})
    result=json.loads(response.content[0].text)
    assert result.get('stdout_log_path')==root+'stdout.log'
    assert result['stderr_log_path']==root+'stderr.log'
    assert result['stdout_log_bytes']==100000 and result['stderr_log_bytes']==0
    assert result['logs_complete'] is complete and result['log_interruption']==reason
    assert response.state==(ToolResultState.SUCCESS if complete else ToolResultState.ERROR)
    assert 'private-host-root' not in response.content[0].text and 'secret' not in response.content[0].text


@pytest.mark.parametrize('path',['/host/private','/workspace/public/execution_logs/../stdout.log',
    '/workspace/public/execution_logs/claim/other.log','/workspace/public/execution_logs/claim/stdout.log?token=x',
    '/workspace/public/execution_logs/claim/stdout.log\nprivate'])
def test_untrusted_log_path_cannot_leak_through_shell_feedback(path):
    response=_sandbox_tool_response('id','execute_shell_command',{'exit_code':0,'stdout':'actual','stderr':'',
        'stdout_log_path':path,'stderr_log_path':path,'stdout_log_bytes':True,'stderr_log_bytes':10**500,
        'logs_complete':'true','log_interruption':'/host/private'})
    result=json.loads(response.content[0].text)
    assert 'stdout_log_path' not in result and 'stderr_log_path' not in result
    assert 'stdout_log_bytes' not in result and 'stderr_log_bytes' not in result
    assert 'logs_complete' not in result and 'log_interruption' not in result


@pytest.mark.parametrize('reason',['sandbox_transport_failed','sandbox_process_cleanup_incomplete',
    'sandbox_output_consumer_failed','sandbox_background_pipes','sandbox_rpc_frame_invalid',
    'sandbox_rpc_frame_too_large','sandbox_rpc_input_too_large','sandbox_command_cancelled','sandbox_log_io_failed'])
def test_only_fixed_new_technical_reasons_survive_failure_projection(reason):
    response=_sandbox_tool_response('new-call','execute_shell_command',{'exit_code':125,'stdout':'partial',
        'stderr':'diagnostic','stdout_bytes':7,'stderr_bytes':10,'technical_reason':reason})
    payload=json.loads(response.content[0].text)
    assert payload['technical_reason']==reason
    assert response.state==ToolResultState.ERROR
    class Rejection:
        status_code=502
        def json(self):return {'detail':{'code':'WORKER_FAILED','details':{'reason':reason,'technical_reason':reason,
            'stdout_bytes':7,'stderr_bytes':10}}}
    error=_technical_rejection(Rejection())
    assert error.reason==reason and error.details['technical_reason']==reason
