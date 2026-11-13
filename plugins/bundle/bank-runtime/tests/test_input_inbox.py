from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from agentscope.message import Msg, TextBlock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bank_runtime.input_inbox import inject_pending_inputs, checkpoint_model_boundary
from bank_runtime.remote_state import EMPTY_INPUT_HASH, RemoteStateClient, RemoteStateError, execution_from_request
from bank_runtime.session import ManagedSessionPrepareHook, ManagedSessionCleanupHook, ManagedSessionCommitHook, current_managed_session_scope
from bank_runtime.events import CompactEventProjector
from qwenpaw.runtime.control import RuntimeSuspension
from test_stateless_execution import context, request


def snapshot(text='补充：只采用已核验的数据'):
    content_hash = hashlib.sha256(json.dumps({
        'previous_hash': EMPTY_INPUT_HASH, 'message_id': 'message-1',
        'request_hash': hashlib.sha256(text.encode()).hexdigest(), 'revision': 1,
    }, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return {'revision': 1, 'content_hash': content_hash, 'consumed_revision': 0,
        'consumed_hash': EMPTY_INPUT_HASH, 'messages': [{'message_id': 'message-1', 'revision': 1,
        'text': text, 'content_hash': content_hash, 'created_at': '2026-11-06T00:00:00Z'}]}


@pytest.mark.asyncio
async def test_model_boundary_checkpoint_is_opt_in_and_rejects_pending_ack(monkeypatch):
    sent = []
    async def post(self, body):
        sent.append(body)
        key = f"{self.execution.execution_id}\0{self.execution.epoch}\0{body['state_hash']}"
        return {'status':'checkpointed', 'state_hash':body['state_hash'],
            'checkpoint_id':'checkpoint_'+hashlib.sha256(key.encode()).hexdigest()}
    monkeypatch.setattr(RemoteStateClient, '_post', post)
    req = request()
    req.runtime_execution['progress_checkpoints'] = True
    ctx = context(req)
    try:
        await ManagedSessionPrepareHook().run(ctx)
        with pytest.raises(RemoteStateError):
            await checkpoint_model_boundary(ctx.agent, SimpleNamespace(pending_acknowledgements={'call'}))
        assert sent == []
        await checkpoint_model_boundary(ctx.agent, SimpleNamespace(pending_acknowledgements=set()))
        assert len(sent) == 1
        assert sent[0]['checkpoint_kind'] == 'progress'
        assert sent[0]['waiting_child_task_ids'] == []
        assert 'private-reasoning' not in json.dumps(sent[0])
    finally:
        await ManagedSessionCleanupHook().run(ctx)
    ctx = context()
    try:
        await ManagedSessionPrepareHook().run(ctx)
        await checkpoint_model_boundary(ctx.agent, None)
        assert len(sent) == 1
    finally:
        await ManagedSessionCleanupHook().run(ctx)


@pytest.mark.asyncio
async def test_input_is_user_message_once_and_hash_is_checked_before_mutation(monkeypatch):
    page = snapshot()
    async def post(self, body):
        assert body['operation'] == 'poll_inputs'
        return {**page, 'messages': page['messages'] if body['after_revision'] == 0 else []}
    monkeypatch.setattr(RemoteStateClient, '_post', post)
    ctx = context()
    ctx.agent.state = SimpleNamespace(context=[Msg(name='user', role='user', content=[TextBlock(text='原始要求')])])
    try:
        await ManagedSessionPrepareHook().run(ctx)
        original = {'messages': list(ctx.agent.state.context)}
        value = await inject_pending_inputs(ctx.agent, original)
        assert len(original['messages']) == 1
        assert len(value['messages']) == 2
        assert value['messages'][-1].role == 'user'
        assert value['messages'][-1].get_text_content() == page['messages'][0]['text']
        assert len(ctx.agent.state.context) == 2
        again = await inject_pending_inputs(ctx.agent, value)
        assert len(again['messages']) == 2
        assert current_managed_session_scope().remote_client.input_hash == page['content_hash']
    finally:
        await ManagedSessionCleanupHook().run(ctx)


@pytest.mark.asyncio
async def test_invalid_chain_leaves_agent_and_cursor_unchanged(monkeypatch):
    async def post(self, body):
        page = snapshot()
        page['messages'][0]['text'] = '未被接受的正文'
        return page
    monkeypatch.setattr(RemoteStateClient, '_post', post)
    ctx = context()
    ctx.agent.state = SimpleNamespace(context=[])
    try:
        await ManagedSessionPrepareHook().run(ctx)
        with pytest.raises(RemoteStateError):
            await inject_pending_inputs(ctx.agent, {'messages': []})
        assert ctx.agent.state.context == []
        assert current_managed_session_scope().remote_client.input_revision == 0
    finally:
        await ManagedSessionCleanupHook().run(ctx)


@pytest.mark.asyncio
async def test_input_race_saves_checkpoint_and_emits_suspension_without_completion(monkeypatch):
    sent = []
    async def post(self, body):
        sent.append(body)
        if body['operation'] == 'stage':
            response = httpx.Response(409, json={'detail': {'code': 'CONFLICT',
                'details': {'reason': 'input_revision_changed'}}})
            return self._response(response)
        assert body['checkpoint_kind'] == 'waiting_inputs'
        assert body['waiting_child_task_ids'] == []
        assert body['input_revision'] == 0 and body['input_hash'] == EMPTY_INPUT_HASH
        return {'checkpoint_id': body['candidate_id'].replace('candidate_', 'checkpoint_'),
            'state_hash': body['state_hash'], 'status': 'checkpointed'}
    monkeypatch.setattr(RemoteStateClient, '_post', post)
    ctx = context()
    try:
        await ManagedSessionPrepareHook().run(ctx)
        with pytest.raises(RuntimeSuspension) as caught:
            await ManagedSessionCommitHook().run(ctx)
        assert caught.value.waiting_inputs
        assert not current_managed_session_scope().commit_allowed
        projector = CompactEventProjector('task-a')
        event = projector.project({'object': 'execution', 'event': 'execution.waiting_inputs',
            'checkpoint_id': caught.value.checkpoint_id, 'waiting_child_task_ids': []})
        assert event[0]['event'] == 'execution.waiting_inputs'
        assert projector.project({'event': 'answer.completed'}) == []
    finally:
        await ManagedSessionCleanupHook().run(ctx)


def test_resumed_input_coverage_must_match_checkpoint():
    req = request()
    req.runtime_execution.update(input_revision=1, input_hash=snapshot()['content_hash'], resume=True)
    req.runtime_execution['context_package']['confirmed_message_ids'] = []
    from bank_runtime.remote_state import canonical_hash
    package = req.runtime_execution['context_package']
    package['hash'] = canonical_hash({key: value for key, value in package.items() if key != 'hash'})
    with pytest.raises(RemoteStateError):
        execution_from_request(req, 'agent-a')
