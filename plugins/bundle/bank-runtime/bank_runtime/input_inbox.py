"""Load accepted task messages at a model boundary without creating a new worker."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from agentscope.message import Msg, TextBlock

from .remote_state import RemoteStateError, _identifier


async def checkpoint_model_boundary(agent: Any, gateway_client: Any) -> None:
    from .session import current_managed_session_scope, _remote_agent_state
    scope = current_managed_session_scope()
    if scope is None or scope.remote_client is None or not scope.remote_client.execution.progress_checkpoints:
        return
    if getattr(gateway_client, 'pending_acknowledgements', None):
        raise RemoteStateError('WORKER_STATE_UNAVAILABLE')
    await scope.remote_client.checkpoint(_remote_agent_state(agent.state_dict()), [], progress=True)


async def inject_pending_inputs(agent: Any, input_kwargs: dict[str, Any]) -> dict[str, Any]:
    from .session import current_managed_session_scope
    scope = current_managed_session_scope()
    if scope is None or scope.remote_client is None:
        return input_kwargs
    client = scope.remote_client
    snapshot = await client._post(client.payload('poll_inputs', after_revision=client.input_revision))
    items = snapshot.get('messages')
    revision, content_hash = client.input_revision, client.input_hash
    if not isinstance(items, list) or len(items) > 1024:
        raise RemoteStateError('WORKER_STATE_INVALID')
    additions = []
    existing = {message.id: message for message in agent.state.context}
    for item in items:
        if (not isinstance(item, dict) or type(item.get('revision')) is not int
                or item['revision'] != revision + 1 or not isinstance(item.get('text'), str)
                or not 1 <= len(item['text']) <= 30000 or not isinstance(item.get('created_at'), str)):
            raise RemoteStateError('WORKER_STATE_INVALID')
        message_id = _identifier(item.get('message_id'))
        revision += 1
        content_hash = hashlib.sha256(json.dumps({
            'previous_hash': content_hash, 'message_id': message_id,
            'request_hash': hashlib.sha256(item['text'].encode('utf-8')).hexdigest(),
            'revision': revision,
        }, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        if content_hash != item.get('content_hash'):
            raise RemoteStateError('WORKER_STATE_CONFLICT')
        message = Msg(name='user', role='user', content=[TextBlock(text=item['text'])])
        message.id = 'runtime_input_' + hashlib.sha256(
            (client.execution.task_id + '\0' + message_id).encode()).hexdigest()
        message.created_at = item['created_at']
        if message.id in existing:
            if existing[message.id].role != 'user' or existing[message.id].get_text_content() != item['text']:
                raise RemoteStateError('WORKER_STATE_CONFLICT')
        else:
            existing[message.id] = message
            additions.append(message)
    if (type(snapshot.get('revision')) is not int or snapshot['revision'] != revision
            or snapshot.get('content_hash') != content_hash):
        raise RemoteStateError('WORKER_STATE_CONFLICT')
    # Mutate only after the entire response is validated. Consumption becomes
    # durable only with the matching C or H commit on Runtime.
    agent.state.context.extend(additions)
    client.input_revision, client.input_hash = revision, content_hash
    if not additions:
        return input_kwargs
    return {**input_kwargs, 'messages': [*input_kwargs.get('messages', []), *additions]}
