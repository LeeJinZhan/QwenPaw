"""Detached bank producers with a Runtime-owned claim and replay ledger."""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any

from .remote_state import RemoteStateClient, RemoteStateError

_log = logging.getLogger(__name__)
# Cancellation handles only. Claims, event ordering and terminal state belong to Runtime.
_LIVE: dict[str, asyncio.Task] = {}
_TERMINALS = {'answer.completed', 'answer.failed', 'execution.waiting', 'execution.waiting_inputs'}


async def _ack(client: RemoteStateClient, body: dict[str, Any]) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + client.execution.lease_budget_seconds
    while True:
        try:
            return await client._post(body)
        except RemoteStateError as exc:
            if exc.http_status in {400, 401, 403, 404, 409, 422} or asyncio.get_running_loop().time() >= deadline:
                raise
            await asyncio.sleep(0.5)


async def _heartbeat(client: RemoteStateClient, producer_id: str, owner: asyncio.Task) -> None:
    while not owner.done():
        await asyncio.sleep(5)
        try:
            result = await _ack(client, client.payload('producer_heartbeat', producer_id=producer_id))
            if result.get('status') != 'active':
                raise RemoteStateError('WORKER_EPOCH_STALE')
        except RemoteStateError:
            owner.cancel('Runtime producer authority lost')
            return


def _decode(frame: str) -> dict[str, Any] | None:
    if not isinstance(frame, str) or not frame.startswith('data:'):
        return None
    value = json.loads(frame[5:].strip())
    if not isinstance(value, dict):
        raise RemoteStateError('WORKER_STATE_INVALID')
    value.pop('_runtime_event_sequence', None)
    value.pop('_runtime_event_terminal', None)
    return value


async def _produce(client: RemoteStateClient, producer_id: str, factory: Callable[[], AsyncIterator[str]]) -> None:
    owner = asyncio.current_task()
    heartbeat = asyncio.create_task(_heartbeat(client, producer_id, owner))
    sequence, terminal = 0, False
    stream = None
    try:
        stream = factory()
        async for frame in stream:
            event = _decode(frame)
            if event is None:
                continue
            sequence += 1
            terminal = event.get('event') in _TERMINALS
            result = await _ack(client, client.payload('append_event', producer_id=producer_id,
                sequence=sequence, event=event, terminal=terminal))
            if result.get('status') != 'persisted' or result.get('sequence') != sequence:
                raise RemoteStateError('WORKER_STATE_CONFLICT')
            if terminal:
                break
        if not terminal:
            raise RemoteStateError('WORKER_STATE_UNAVAILABLE')
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _log.warning('Bank producer stopped: execution=%s error_type=%s', client.execution.execution_id, type(exc).__name__)
        if not terminal:
            try:
                event = {'event': 'answer.failed', 'task_id': client.execution.task_id,
                    'payload': {'code': getattr(exc, 'error_code', 'WORKER_STATE_UNAVAILABLE'),
                        'message': '执行状态需要核验。'}}
                await _ack(client, client.payload('append_event', producer_id=producer_id,
                    sequence=sequence + 1, event=event, terminal=True))
            except Exception:
                pass  # Runtime producer lease records uncertainty; no local success.
    finally:
        if stream is not None and hasattr(stream, 'aclose'):
            await stream.aclose()
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)
        if _LIVE.get(client.execution.run_key) is owner:
            _LIVE.pop(client.execution.run_key, None)


async def open_execution_stream(client: RemoteStateClient, factory: Callable[[], AsyncIterator[str]],
                                *, after_sequence: int = 0) -> AsyncIterator[str]:
    if type(after_sequence) is not int or not 0 <= after_sequence <= 2**63 - 1:
        raise RemoteStateError('WORKER_STATE_INVALID')
    producer_id = 'producer_' + uuid.uuid4().hex
    capacity = ({'model_capacity_protocol':'shared-model-v1'}
        if getattr(client.execution,'model_capacity_enabled',False) else {})
    claim = await _ack(client, client.payload('claim_producer', producer_id=producer_id, **capacity))
    if type(claim.get('producer')) is not bool:
        raise RemoteStateError('WORKER_STATE_INVALID')
    if claim['producer']:
        if claim.get('producer_id') != producer_id:
            raise RemoteStateError('WORKER_SCOPE_MISMATCH')
        # This coroutine has one UUID and starts exactly once after ACK. Retrying
        # the claim uses that UUID; other HTTP consumers receive producer=false.
        task = asyncio.create_task(_produce(client, producer_id, factory))
        _LIVE[client.execution.run_key] = task

    async def consume() -> AsyncIterator[str]:
        cursor = after_sequence
        while True:
            page = await _ack(client, client.payload('read_events', after_sequence=cursor, limit=100))
            items = page.get('items')
            if not isinstance(items, list) or len(items) > 100:
                raise RemoteStateError('WORKER_STATE_INVALID')
            for item in items:
                if (not isinstance(item, dict) or type(item.get('sequence')) is not int or item['sequence'] != cursor + 1
                        or not isinstance(item.get('event'), dict) or type(item.get('terminal')) is not bool):
                    raise RemoteStateError('WORKER_STATE_CONFLICT')
                cursor = item['sequence']
                yield 'data: ' + json.dumps({**item['event'], '_runtime_event_sequence': cursor,
                    '_runtime_event_terminal': item['terminal']}, ensure_ascii=False) + '\n\n'
                if item['terminal']:
                    return
            if page.get('status') == 'done':
                return
            if page.get('status') in {'expired', 'lost', 'uncertain', 'cancelled', 'failed'}:
                raise RemoteStateError('WORKER_STATE_UNAVAILABLE')
            if not items:
                await asyncio.sleep(0.25)
    return consume()


def stop_local_execution(run_key: str) -> bool:
    task = _LIVE.get(run_key)
    if task is None or task.done():
        return False
    task.cancel('Runtime requested execution stop')
    return True
