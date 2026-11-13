import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bank_runtime.execution_stream import open_execution_stream, _LIVE
from bank_runtime.remote_state import RemoteStateClient, RemoteStateError, execution_from_request
from test_stateless_execution import request


@pytest.mark.asyncio
@pytest.mark.parametrize('capacity_enabled',[False,True])
async def test_lost_claim_ack_and_http_detach_attach_never_restart_producer(monkeypatch,capacity_enabled):
    owner, events, status = '', [], 'active'
    lost = True
    claims = []
    async def post(self, body):
        nonlocal owner, status, lost
        op = body['operation']
        if op == 'claim_producer':
            assert body.get('model_capacity_protocol') == ('shared-model-v1' if capacity_enabled else None)
            claims.append(body['producer_id'])
            owner = owner or body['producer_id']
            if lost:
                lost = False
                raise RemoteStateError()
            return {'producer': owner == body['producer_id'], 'producer_id': owner, 'status': status, 'last_sequence': len(events)}
        if op == 'append_event':
            assert body['producer_id'] == owner
            assert body['sequence'] == len(events) + 1
            events.append({'sequence': body['sequence'], 'event': body['event'], 'terminal': body['terminal']})
            if body['terminal']:
                status = 'done'
            return {'sequence': len(events), 'status': 'persisted'}
        if op == 'read_events':
            return {'items': events[body['after_sequence']:], 'status': status, 'last_sequence': len(events)}
        if op == 'producer_heartbeat':
            return {'status': 'active'}
        pytest.fail('Unexpected ledger operation')
    monkeypatch.setattr(RemoteStateClient, '_post', post)
    gate = asyncio.Event()
    starts = 0
    async def producer():
        nonlocal starts
        starts += 1
        yield 'data: ' + json.dumps({'event': 'answer.started', 'payload': {}}) + '\n\n'
        await gate.wait()
        yield 'data: ' + json.dumps({'event': 'answer.completed', 'payload': {'text': 'ok'}}) + '\n\n'
    payload = request()
    payload.runtime_execution['model_capacity_enabled'] = capacity_enabled
    execution = execution_from_request(payload, 'agent-a')
    first = await open_execution_stream(RemoteStateClient(execution), producer)
    item = json.loads((await anext(first))[5:])
    assert item['_runtime_event_sequence'] == 1 and not item['_runtime_event_terminal']
    await first.aclose()  # HTTP detaches while the producer remains alive.
    assert not _LIVE[execution.run_key].done()
    async def forbidden():
        pytest.fail('Duplicate attempt constructed a new agent')
        yield ''
    second = await open_execution_stream(RemoteStateClient(execution), forbidden, after_sequence=1)
    gate.set()
    remainder = [json.loads(frame[5:]) async for frame in second]
    assert starts == 1 and claims[0] == claims[1] and claims[-1] != claims[0]
    assert len(remainder) == 1 and remainder[0]['_runtime_event_terminal'] is True
    assert remainder[0]['_runtime_event_sequence'] == 2
    if execution.run_key in _LIVE:
        await _LIVE[execution.run_key]
