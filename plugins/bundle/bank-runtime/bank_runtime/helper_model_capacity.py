"""Admission for tool-free inference; permits never enter model messages."""
import asyncio
from collections.abc import AsyncIterable
from contextlib import suppress
import hashlib
import hmac
import json
import os
import time
import uuid
from urllib.parse import urlsplit

import httpx

from .remote_state import canonical_hash, RemoteStateError


def govern_helper_model(model, agent_id, purpose, ticket):
    if ticket is None:
        if os.environ.get('BANK_RUNTIME_MODEL_CAPACITY_REQUIRED', '').lower() in {'1', 'true', 'yes'}:
            raise RemoteStateError('HELPER_MODEL_CAPACITY_REQUIRED')
        return model
    secret = os.environ.get('QWENPAW_SERVICE_TOKEN', '').strip()
    signed = {key: value for key, value in ticket.items() if key != 'signature'}
    signature = hmac.new(secret.encode(), json.dumps(signed, sort_keys=True, separators=(',', ':')).encode(), hashlib.sha256).hexdigest()
    parsed = urlsplit(str(ticket.get('base_url', '')))
    if (not secret or not isinstance(ticket.get('signature'), str)
            or not hmac.compare_digest(signature, ticket['signature'])
            or ticket.get('agent_id') != agent_id or ticket.get('purpose') != purpose
            or ticket.get('version') != 1 or type(ticket.get('expires_at')) is not int
            or ticket['expires_at'] <= time.time() or ticket['expires_at'] > time.time()+171
            or parsed.scheme not in {'http', 'https'} or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise RemoteStateError('HELPER_MODEL_CAPACITY_INVALID')
    return GovernedHelperModel(model, ticket, secret)


class GovernedHelperModel:
    def __init__(self, model, ticket, secret):
        self.model, self.ticket, self.secret = model, dict(ticket), secret
        self.used = False

    def __getattr__(self, name):
        return getattr(self.model, name)

    async def _callback(self, body):
        async with httpx.AsyncClient(timeout=3, trust_env=False, follow_redirects=False) as client:
            response = await client.post(self.ticket['base_url']+'/runtime/v1/helper-model-capacity',
                headers={'Authorization': 'Bearer '+self.secret, 'X-Agent-Id': self.ticket['agent_id']},
                json={'ticket': self.ticket, **body})
            response.raise_for_status()
            return response.json()

    async def __call__(self, messages, **kwargs):
        from .context_budget import bound, configured_budget
        if self.used or kwargs.get('tools') or kwargs.get('stream') is not False:
            raise RemoteStateError('HELPER_MODEL_CAPACITY_INVALID')
        self.used = True
        provider_id, model_id, provider = self.model._bank_capacity_source
        limit, _ = configured_budget(provider.get_model_info(model_id), provider.get_effective_generate_kwargs(model_id))
        output = kwargs.get(getattr(self.model, '_output_token_param', 'max_tokens'))
        if type(output) is not int or output <= 0:
            raise RemoteStateError('HELPER_MODEL_CAPACITY_INVALID')
        material = [message.to_dict() if hasattr(message, 'to_dict') else message for message in messages]
        reserve = bound({'messages': material, 'tools': []}) + output + 1024
        if reserve > limit:
            raise RemoteStateError('WORKER_CONTEXT_BUDGET_EXCEEDED')
        producer = uuid.uuid4().hex
        acquire = dict(operation='acquire', producer_id=producer, context_limit=limit, reserved_tokens=reserve,
            model_key=canonical_hash({'provider':provider_id, 'model':model_id,
                'endpoint':str(getattr(provider, 'base_url', '')).rstrip('/')}))
        async def settle(outcome):
            with suppress(Exception):
                async with asyncio.timeout(1):
                    await self._callback(dict(operation='release', producer_id=producer, outcome=outcome))
        async with asyncio.timeout(max(0.001, self.ticket['expires_at']-time.time())):
            while True:
                receipt = await self._callback(acquire)
                if receipt.get('request_id') != self.ticket['request_id']:
                    raise RemoteStateError('HELPER_MODEL_CAPACITY_INVALID')
                if receipt.get('status') == 'active':
                    break
                if receipt.get('status') != 'waiting':
                    raise RemoteStateError('HELPER_MODEL_CAPACITY_UNAVAILABLE')
                await asyncio.sleep(0.5)
            outcome = 'failed'
            handed_to_stream = False
            try:
                result = await self.model(messages, **kwargs)
                if isinstance(result, AsyncIterable):
                    # Some SDKs return an iterator even for stream=False.
                    # Preserve text/usage chunks and hold admission until the
                    # consumer finishes; an empty final usage chunk is not text.
                    async def stream():
                        terminal = 'failed'
                        try:
                            async with asyncio.timeout(max(.001,self.ticket['expires_at']-time.time())):
                                async for chunk in result:
                                    yield chunk
                            terminal = 'completed'
                        except (asyncio.CancelledError,GeneratorExit):
                            terminal = 'cancelled'
                            raise
                        finally:
                            if hasattr(result,'aclose'):
                                with suppress(Exception):
                                    await result.aclose()
                            await settle(terminal)
                    handed_to_stream = True
                    return stream()
                outcome = 'completed'
                return result
            except asyncio.CancelledError:
                outcome = 'cancelled'
                raise
            finally:
                if not handed_to_stream:
                    await settle(outcome)
