import asyncio
from types import SimpleNamespace
from pathlib import Path
import sys
import time
from unittest.mock import AsyncMock

import httpx
import pytest
from agentscope.message import TextBlock
from agentscope.model import ChatResponse

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from bank_runtime.model_capacity import govern_model_handler
from bank_runtime.model_reliability import BankModelReliability
from bank_runtime.remote_state import RemoteStateError


@pytest.fixture
def client(monkeypatch):
    client = SimpleNamespace(execution=SimpleNamespace(model_capacity_enabled=True,
        context_package={'budget_evidence':{'output_reserve_tokens':100}}), release_model=AsyncMock())
    async def acquire(request_id, timeout_seconds):
        return {'request_id':request_id,'status':'active'}
    client.acquire_model = AsyncMock(side_effect=acquire)
    monkeypatch.setattr('bank_runtime.session.current_managed_session_scope',lambda:SimpleNamespace(remote_client=client))
    return client


@pytest.mark.asyncio
async def test_stream_holds_reservation_until_consumed_and_closes_on_cancel(client):
    async def output():
        yield 'first'
        await asyncio.sleep(10)
    model = AsyncMock(return_value=output())
    stream = await govern_model_handler(model,time.monotonic()+30)(max_tokens=100)
    assert await anext(stream) == 'first'
    client.release_model.assert_not_called()
    await stream.aclose()
    assert client.release_model.call_args.args[1] == 'cancelled'


@pytest.mark.asyncio
async def test_every_reliability_retry_gets_its_own_shared_reservation(client):
    policy = BankModelReliability(10)
    model = AsyncMock(side_effect=[httpx.ConnectError('unavailable'),ChatResponse([TextBlock(text='done')],True)])
    guarded = govern_model_handler(model,policy.deadline)
    chunks = [chunk async for chunk in await policy.call(guarded, messages=[],tools=[])]
    assert chunks[-1].content[0].text == 'done'
    assert client.acquire_model.call_count == 2
    assert client.acquire_model.call_args_list[0].args[0] != client.acquire_model.call_args_list[1].args[0]
    assert [call.args[1] for call in client.release_model.call_args_list] == ['failed','completed']


@pytest.mark.asyncio
async def test_denied_budget_never_calls_provider_and_does_not_retry_with_a_new_id(client):
    async def reject(request_id, timeout_seconds):
        return {'request_id':request_id,'status':'rejected','reason':'task_token_budget_exhausted'}
    client.acquire_model.side_effect = reject
    model = AsyncMock()
    with pytest.raises(RemoteStateError):
        await govern_model_handler(model,time.monotonic()+10)()
    model.assert_not_called()
    assert client.acquire_model.call_count == 1


@pytest.mark.asyncio
async def test_output_override_cannot_exceed_the_reserved_context(client):
    model = AsyncMock()
    with pytest.raises(RemoteStateError):
        await govern_model_handler(model,time.monotonic()+10)(max_tokens=101)
    model.assert_not_called()
    client.acquire_model.assert_not_called()


@pytest.mark.asyncio
async def test_release_ack_failure_preserves_result_without_repeating_model(client):
    client.release_model.side_effect = ConnectionError('lost ack')
    model = AsyncMock(return_value='done')
    assert await govern_model_handler(model,time.monotonic()+10)() == 'done'
    assert model.call_count == 1


@pytest.mark.asyncio
async def test_capacity_wait_reuses_admission_id_and_keeps_provider_idle(client):
    observed = []
    async def acquire(request_id, timeout_seconds):
        observed.append(request_id)
        return {'request_id':request_id,'status':'waiting' if len(observed)==1 else 'active'}
    client.acquire_model.side_effect = acquire
    model = AsyncMock(return_value='done')
    assert await govern_model_handler(model,time.monotonic()+5)() == 'done'
    assert len(observed) == 2 and len(set(observed)) == 1
    assert model.call_count == 1
