import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from types import SimpleNamespace
import pytest
from agentscope.message import TextBlock
from agentscope.model import ChatResponse
from bank_runtime.artifact_tools import ArtifactDeliveryIntent, ArtifactToolNotInvokedError
from bank_runtime.gateway.middleware import BankRuntimeGatewayMiddleware
from bank_runtime.pptx_draft import controlled_pptx_call

PAYLOAD = {'title':'初稿', 'content':{'theme':'steady_business','slides':[{'layout':'cover','title':'初稿'},{'layout':'content','title':'待补充内容','bullets':['主题待补充']}]}}
def response(value=PAYLOAD, final=True):
    return ChatResponse(content=[TextBlock(text=json.dumps(value,ensure_ascii=False))],is_last=final)

def test_complete_draft_becomes_only_an_unexecuted_tool_proposal():
    result=controlled_pptx_call(response())
    assert result.content[0].name == 'artifact_generate'
    assert json.loads(result.content[0].input) == {'artifact_type':'pptx',**PAYLOAD}

@pytest.mark.parametrize('value', [{'title':'x','content':{'slides':[]}}, {**PAYLOAD,'source_refs':['invented']}, {'title':'x','content':'text'}])
def test_invalid_draft_is_rejected(value):
    with pytest.raises(ValueError): controlled_pptx_call(response(value))

@pytest.mark.asyncio
async def test_missing_tool_call_recovers_once_and_remains_subject_to_gateway():
    mw=BankRuntimeGatewayMiddleware(None,artifact_intent=ArtifactDeliveryIntent('generate','pptx'))
    mw._artifact_turn_state=SimpleNamespace(invoked=False,failed=False,replan_count=0)
    calls=[]
    async def model(**kwargs):
        calls.append(kwargs)
        return response('准备生成') if len(calls)<3 else response()
    result=await mw.on_model_call(None,{'tools':[{'function':{'name':'artifact_generate'}}]},model)
    assert result.content[0].name=='artifact_generate'
    assert len(calls)==3
    assert calls[-1]['tools']==[]
    assert calls[-1]['tool_choice'].mode=='none'
    assert 'speaker_notes' in calls[-1]['messages'][-1].get_text_content()
    assert not mw._prepared

@pytest.mark.asyncio
async def test_invalid_recovery_is_not_repeated():
    mw=BankRuntimeGatewayMiddleware(None,artifact_intent=ArtifactDeliveryIntent('generate','pptx'))
    mw._artifact_turn_state=SimpleNamespace(invoked=False,failed=False,replan_count=0)
    calls=[]
    async def model(**kwargs):
        calls.append(kwargs);return response('无法生成')
    kwargs={'tools':[{'function':{'name':'artifact_generate'}}]}
    for _ in range(2):
        with pytest.raises(ArtifactToolNotInvokedError): await mw.on_model_call(None,kwargs,model)
    assert len(calls)==3

@pytest.mark.parametrize('final',[False])
def test_incomplete_stream_cannot_become_a_file(final):
    with pytest.raises(ValueError): controlled_pptx_call(response(final=final))

@pytest.mark.asyncio
@pytest.mark.parametrize('reason', ['length','error','content_filter'])
async def test_truncated_stream_recovery_is_bounded_and_filter_is_not_bypassed(reason):
    from qwenpaw.exceptions import ModelExecutionException
    mw=BankRuntimeGatewayMiddleware(None,artifact_intent=ArtifactDeliveryIntent('generate','pptx'))
    mw._artifact_turn_state=SimpleNamespace(invoked=False,failed=False,replan_count=0)
    calls=[]
    async def model(**kwargs):
        calls.append(kwargs)
        if len(calls)==1: raise ModelExecutionException('test',details={'finish_reason':reason})
        return response()
    kwargs={'tools':[{'function':{'name':'artifact_generate'}}]}
    if reason=='content_filter':
        with pytest.raises(ModelExecutionException): await mw.on_model_call(None,kwargs,model)
        assert len(calls)==1
    else:
        result=await mw.on_model_call(None,kwargs,model)
        assert result.content[0].name=='artifact_generate'
        assert len(calls)==2

@pytest.mark.asyncio
@pytest.mark.parametrize('operation,sources,visible,unresolved', [
    ('generate',('file1',),True,False), ('revise',(),True,False),
    ('generate',(),False,False), ('generate',(),True,True)])
async def test_recovery_does_not_expand_authority(operation,sources,visible,unresolved):
    mw=BankRuntimeGatewayMiddleware(None,artifact_intent=ArtifactDeliveryIntent(operation,'pptx',source_refs=sources))
    mw._artifact_turn_state=SimpleNamespace(invoked=False,failed=False,replan_count=0)
    if unresolved: mw.unresolved_file_operations.add('file1')
    async def forbidden(**kwargs): pytest.fail('must not request a recovery draft')
    result=await mw._recover_pptx_proposal({'tools':[{'function':{'name':'artifact_generate'}}] if visible else []}, forbidden,mw._artifact_turn_state)
    assert result is None

@pytest.mark.asyncio
async def test_recovered_proposal_cannot_bypass_preflight_denial():
    from agentscope.permission import PermissionBehavior
    from bank_runtime.gateway.client import GatewayError
    from bank_runtime.gateway.middleware import GatewayPermissionEngine
    class DeniedClient:
        async def preflight(self,*args,**kwargs): raise GatewayError('denied',code='POLICY_BLOCKED')
    class UnusedGuard:
        context=None
        async def check_permission(self,*args,**kwargs): pytest.fail('denied preflight must stop')
    mw=BankRuntimeGatewayMiddleware(DeniedClient(),artifact_intent=ArtifactDeliveryIntent('generate','pptx'))
    proposal=controlled_pptx_call(response())
    decision=await GatewayPermissionEngine(UnusedGuard(),mw).check_permission(SimpleNamespace(name='artifact_generate'),json.loads(proposal.content[0].input))
    assert decision.behavior==PermissionBehavior.DENY
    assert not mw._prepared

@pytest.mark.parametrize('prefix,suffix',[('```json\n','\n```'),('以下是初稿参数：\n','\n请补充主题。')])
def test_single_complete_proposal_ignores_non_artifact_narration(prefix,suffix):
    draft=ChatResponse(content=[TextBlock(text=prefix+json.dumps(PAYLOAD)+suffix)],is_last=True)
    assert json.loads(controlled_pptx_call(draft).content[0].input)['content']==PAYLOAD['content']

def test_multiple_or_truncated_proposals_are_never_repaired_by_guessing():
    for text in (json.dumps(PAYLOAD)+'\n'+json.dumps(PAYLOAD),json.dumps(PAYLOAD)[:-2]):
        with pytest.raises(ValueError): controlled_pptx_call(ChatResponse(content=[TextBlock(text=text)],is_last=True))
