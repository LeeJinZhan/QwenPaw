import asyncio
import hashlib
import hmac
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest
from agentscope.message import Msg

from bank_runtime.helper_model_capacity import govern_helper_model, GovernedHelperModel
from bank_runtime.remote_state import RemoteStateError


def permit(purpose='context-summary'):
    value = dict(version=1,agent_id='agent',purpose=purpose,expires_at=int(time.time())+60,
        base_url='http://runtime.test',request_id='model_'+'a'*32)
    value['signature'] = hmac.new(b'test-helper',json.dumps(value,sort_keys=True,separators=(',',':')).encode(),hashlib.sha256).hexdigest()
    return value


class Model:
    def __init__(self):
        provider = SimpleNamespace(base_url='https://provider.test',
            get_model_info=lambda _:SimpleNamespace(max_input_length=32768,max_tokens=4096,max_input_length_configured=True),
            get_effective_generate_kwargs=lambda _: {})
        self._bank_capacity_source = ('provider','model',provider)
        self.calls = []

    async def __call__(self,messages,**kwargs):
        self.calls.append((messages,kwargs))
        return {'content':'summary','usage':{'output_tokens':10}}


@pytest.mark.asyncio
async def test_one_physical_call_waits_with_same_identity_and_releases(monkeypatch):
    monkeypatch.setenv('QWENPAW_SERVICE_TOKEN','test-helper')
    model, ticket = Model(), permit()
    governed = govern_helper_model(model,'agent','context-summary',ticket)
    callback = AsyncMock(side_effect=[{'request_id':ticket['request_id'],'status':'waiting'},
        {'request_id':ticket['request_id'],'status':'active'},{'released':True}])
    monkeypatch.setattr(governed,'_callback',callback)
    result = await governed([Msg(name='history',content=[{'type':'text','text':'Facts'}],role='user')],tools=[],stream=False,max_tokens=256)
    assert result['content']=='summary' and len(model.calls)==1
    bodies = [call.args[0] for call in callback.call_args_list]
    assert bodies[0]==bodies[1] and bodies[0]['reserved_tokens']>256
    assert bodies[2]['outcome']=='completed'
    assert 'signature' not in str(model.calls)
    with pytest.raises(RemoteStateError):
        await governed([],tools=[],stream=False,max_tokens=256)
    assert len(model.calls)==1


@pytest.mark.asyncio
async def test_denied_or_oversized_budget_never_calls_model(monkeypatch):
    monkeypatch.setenv('QWENPAW_SERVICE_TOKEN','test-helper')
    for oversized in (True,False):
        model,ticket=Model(),permit()
        governed=govern_helper_model(model,'agent','context-summary',ticket)
        callback=AsyncMock(return_value={'request_id':ticket['request_id'],'status':'rejected'})
        monkeypatch.setattr(governed,'_callback',callback)
        with pytest.raises(RemoteStateError):
            await governed([Msg(name='history',content=[{'type':'text','text':'x'*40000 if oversized else 'facts'}],role='user')],tools=[],stream=False,max_tokens=256)
        assert not model.calls
        assert callback.call_count == (0 if oversized else 1)


def test_unsigned_cross_agent_and_required_missing_ticket_are_rejected(monkeypatch):
    monkeypatch.setenv('QWENPAW_SERVICE_TOKEN','test-helper')
    for ticket,agent in [(dict(permit(),base_url='http://attacker.test'),'agent'),(permit(),'another')]:
        with pytest.raises(RemoteStateError):
            govern_helper_model(Model(),agent,'context-summary',ticket)
    monkeypatch.setenv('BANK_RUNTIME_MODEL_CAPACITY_REQUIRED','true')
    with pytest.raises(RemoteStateError):
        govern_helper_model(Model(),'agent','context-summary',None)


@pytest.mark.asyncio
async def test_cancellation_releases_and_lost_ack_does_not_retry_model(monkeypatch):
    monkeypatch.setenv('QWENPAW_SERVICE_TOKEN','test-helper')
    started=asyncio.Event()
    class Blocking(Model):
        async def __call__(self,*args,**kwargs):
            started.set()
            await asyncio.Event().wait()
    ticket=permit()
    governed=govern_helper_model(Blocking(),'agent','context-summary',ticket)
    callback=AsyncMock(side_effect=[{'request_id':ticket['request_id'],'status':'active'},RuntimeError('lost ack')])
    monkeypatch.setattr(governed,'_callback',callback)
    task=asyncio.create_task(governed([],tools=[],stream=False,max_tokens=256))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert callback.call_args_list[-1].args[0]['outcome']=='cancelled'


@pytest.mark.asyncio
async def test_summary_and_review_never_put_permit_in_model_material(monkeypatch):
    from bank_runtime import context_summary as summary, content_review as review
    monkeypatch.setenv('QWENPAW_SERVICE_TOKEN','test-helper')
    models=[]
    class ReviewModel(Model):
        async def __call__(self,messages,**kwargs):
            self.calls.append((messages,kwargs))
            return {'content':'{"classification":"safe_content","risk_codes":[],"evidence":[]}'}
    async def callback(self,body):
        return {'request_id':self.ticket['request_id'],'status':'active','released':True}
    monkeypatch.setattr(GovernedHelperModel,'_callback',callback)
    for module,model in [(summary,Model()),(review,ReviewModel())]:
        models.append(model)
        monkeypatch.setattr(module,'create_review_model',lambda _, model=model:model)
        monkeypatch.setattr(module,'require_review_agent',lambda _:None)
    await summary.summarize('agent',summary.SummaryRequest(messages=[{'role':'user','content':'facts'}],
        source_revision=1,source_hash='a'*64,max_summary_tokens=256,runtime_model_capacity=permit()))
    await review.review_content('agent',review.ReviewRequest(text='facts',runtime_model_capacity=permit('content-review')))
    assert all(len(model.calls)==1 for model in models)
    assert all('signature' not in str(model.calls) and 'runtime_model_capacity' not in str(model.calls) for model in models)


@pytest.mark.asyncio
async def test_nonstream_sdk_iterator_preserves_text_and_final_usage_until_consumed(monkeypatch):
    monkeypatch.setenv('QWENPAW_SERVICE_TOKEN','test-helper')
    class IteratorModel(Model):
        async def __call__(self,*args,**kwargs):
            async def chunks():
                yield {'content':'facts'}
                yield {'content':'','usage':{'output_tokens':3}}
            return chunks()
    ticket=permit()
    governed=govern_helper_model(IteratorModel(),'agent','context-summary',ticket)
    callback=AsyncMock(side_effect=[{'request_id':ticket['request_id'],'status':'active'},{'released':True}])
    monkeypatch.setattr(governed,'_callback',callback)
    chunks=await governed([],tools=[],stream=False,max_tokens=256)
    assert callback.call_count==1
    assert [chunk async for chunk in chunks]==[{'content':'facts'},{'content':'','usage':{'output_tokens':3}}]
    assert callback.call_args_list[-1].args[0]['outcome']=='completed'
