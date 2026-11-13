"""Shared Runtime admission around every physical bank model invocation."""
import asyncio
from collections.abc import AsyncIterable
from contextlib import suppress
import time
import uuid
import os

from .remote_state import RemoteStateError


def govern_model_handler(handler, deadline):
    from .session import current_managed_session_scope
    managed = current_managed_session_scope()
    client = managed.remote_client if managed is not None else None
    if client is None or not getattr(client.execution,'model_capacity_enabled',False):
        if os.environ.get('BANK_RUNTIME_MODEL_CAPACITY_REQUIRED','').lower() in {'1','true','yes'}:
            raise RemoteStateError('WORKER_MODEL_CAPACITY_REQUIRED')
        return handler

    async def invoke(**kwargs):
        evidence = client.execution.context_package.get('budget_evidence') or {}
        output = evidence.get('output_reserve_tokens')
        if type(output) is not int or output <= 0:
            raise RemoteStateError('WORKER_CONTEXT_BUDGET_UNAVAILABLE')
        for field in ('max_tokens','max_completion_tokens','max_output_tokens'):
            value = kwargs.get(field)
            if value is not None and (type(value) is not int or not 1 <= value <= output):
                raise RemoteStateError('WORKER_CONTEXT_BUDGET_EXCEEDED')
        request_id = 'model_' + uuid.uuid4().hex
        async def settle(outcome):
            with suppress(Exception):
                async with asyncio.timeout(1):
                    await client.release_model(request_id,outcome)
        timeout = max(1,min(3600,int(deadline-time.monotonic())))
        while True:
            if time.monotonic() >= deadline:
                raise RemoteStateError('WORKER_CONTEXT_BUDGET_EXCEEDED')
            receipt = await client.acquire_model(request_id,timeout)
            if receipt.get('request_id') != request_id:
                raise RemoteStateError('WORKER_STATE_INVALID')
            if receipt.get('status') == 'active':
                break
            if receipt.get('status') != 'waiting':
                raise RemoteStateError('WORKER_CONTEXT_BUDGET_EXCEEDED')
            await asyncio.sleep(min(1,max(0,deadline-time.monotonic())))
        try:
            result = await handler(**kwargs)
        except BaseException:
            await settle('failed')
            raise
        if not isinstance(result,AsyncIterable):
            # A failed confirmation leaves a conservative reservation until its
            # original deadline; never invoke the model again to repair an ACK.
            await settle('completed')
            return result

        async def stream():
            outcome = 'failed'
            try:
                async for chunk in result:
                    yield chunk
                outcome = 'completed'
            except (asyncio.CancelledError,GeneratorExit):
                outcome = 'cancelled'
                raise
            finally:
                if hasattr(result,'aclose'):
                    with suppress(Exception):
                        await result.aclose()
                await settle(outcome)
        return stream()
    return invoke
