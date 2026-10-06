from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from bank_runtime.gateway.client import GatewayClient, GatewayConfig, GatewayError
from bank_runtime.gateway.outbox import GatewayResultOutbox
from bank_runtime.gateway.protocol import canonical_payload_hash


def _config() -> GatewayConfig:
    return GatewayConfig.from_mapping(
        {
            "protocol": "preflight_guard_result_v2",
            "base_url": "http://127.0.0.1:8765",
            "endpoint": "/runtime/v1/tool-calls",
            "token": "worker-secret",
            "task_id": "task_001",
            "session_id": "session_001",
            "tool_session_id": "wts_001",
            "policy_snapshot_id": "policy_001",
            "task_scope_id": "scope_001",
            "capability_snapshot_hash": "sha256:capability",
            "worker_protocol_version": "runtime-worker/v1",
            "trace_id": "trace_001",
            "worker_agent_id": "bank-assistant",
        }
    )


def _permit(tool_input, *, agent_id="bank-assistant", tool_id="policy_search") -> dict:
    return {
        "protocol_version": "runtime-worker/v1",
        "message_type": "tool.permit",
        "task_scope_id": "scope_001",
        "trace_id": "trace_001",
        "call_id": "model_call_001",
        "idempotency_key": "qwenpaw:model_call_001",
        "capability_snapshot_hash": "sha256:capability",
        "payload": {
            "permit_id": "permit_001",
            "permit_nonce": "nonce-once",
            "task_id": "task_001",
            "agent_id": agent_id,
            "tool_id": tool_id,
            "input_hash": canonical_payload_hash(tool_input),
            "expires_at": (
                datetime.now(timezone.utc) + timedelta(seconds=30)
            ).isoformat(),
            "single_use": True,
        },
    }


@pytest.mark.asyncio
async def test_client_keeps_one_correlation_across_preflight_guard_result(
    tmp_path,
    monkeypatch,
) -> None:
    client = GatewayClient(_config(), outbox=GatewayResultOutbox(tmp_path))
    requests = []
    tool_input = {"query": "制度"}

    async def post(payload):
        requests.append(payload)
        if payload["phase"] == "preflight":
            return {
                "phase": "allow",
                "decision": "allow",
                "status": "allowed",
                "tool_call_id": "runtime_call_001",
                "permit": _permit(tool_input),
            }
        if payload["phase"] == "guard":
            return {
                "tool_call_id": "runtime_call_001",
                "guard_decision": "allow",
                "status": "executing",
            }
        return {"tool_call_id": "runtime_call_001", "status": "completed"}

    monkeypatch.setattr(client, "_post", post)
    preflight = await client.preflight(
        "policy_search",
        tool_input,
        call_id="model_call_001",
    )
    await client.report_guard(preflight, "allow")
    await client.report_result("runtime_call_001", "completed", 9)

    assert [request["phase"] for request in requests] == [
        "preflight",
        "guard",
        "result",
    ]
    assert requests[0]["call_id"] == "model_call_001"
    assert requests[0]["input_hash"] == canonical_payload_hash(tool_input)
    assert requests[1]["permit_id"] == "permit_001"
    assert requests[2]["call_id"] == "runtime_call_001"
    assert all(request["worker_agent_id"] == "bank-assistant" for request in requests)
    assert not list(tmp_path.glob("*.json"))


@pytest.mark.asyncio
async def test_client_executes_runtime_native_tool_with_same_permit_and_input(
    monkeypatch,
) -> None:
    client = GatewayClient(_config())
    tool_input = {"artifact_type": "docx", "title": "纪要", "content": {}}
    requests = []

    async def post(payload):
        requests.append(payload)
        return {
            "tool_call_id": "runtime_call_001",
            "status": "success",
            "result": {"artifact_job_id": "artifact_job_001"},
        }

    monkeypatch.setattr(client, "_post", post)
    preflight = {
        "tool_call_id": "runtime_call_001",
        "call_id": "model_call_001",
        "idempotency_key": "qwenpaw:model_call_001",
        "permit": _permit(tool_input, tool_id="artifact_generate"),
    }

    result = await client.execute_runtime_tool(
        preflight,
        "artifact_generate",
        tool_input,
    )

    assert result["result"]["artifact_job_id"] == "artifact_job_001"
    assert requests == [
        {
            "phase": "execute",
            "task_id": "task_001",
            "session_id": "session_001",
            "tool_session_id": "wts_001",
            "policy_snapshot_id": "policy_001",
            "worker_agent_id": "bank-assistant",
            "worker_tool_name": "artifact_generate",
            "input": tool_input,
            "tool_call_id": "runtime_call_001",
            "permit_id": "permit_001",
            "protocol_version": "runtime-worker/v1",
            "message_type": "tool.intent",
            "task_scope_id": "scope_001",
            "trace_id": "trace_001",
            "call_id": "model_call_001",
            "idempotency_key": "qwenpaw:model_call_001",
            "capability_snapshot_hash": "sha256:capability",
            "input_hash": canonical_payload_hash(tool_input),
            "action_type": "execute",
        }
    ]


@pytest.mark.asyncio
async def test_client_rejects_runtime_permit_with_wrong_agent(monkeypatch) -> None:
    client = GatewayClient(_config())
    tool_input = {"query": "制度"}

    async def post(_payload):
        return {
            "phase": "allow",
            "decision": "allow",
            "status": "allowed",
            "tool_call_id": "runtime_call_001",
            "permit": _permit(tool_input, agent_id="other-agent"),
        }

    monkeypatch.setattr(client, "_post", post)
    with pytest.raises(GatewayError):
        await client.preflight(
            "policy_search",
            tool_input,
            call_id="model_call_001",
        )

@pytest.mark.asyncio
async def test_execution_transport_waits_for_document_worker_but_control_calls_remain_short(monkeypatch):
    import httpx
    timeouts = []
    class Transport:
        def __init__(self, **kwargs):
            timeouts.append(httpx.Timeout(kwargs['timeout']).read)
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, **kwargs):
            return httpx.Response(200, json={'status': 'success'})
    monkeypatch.setattr(httpx, 'AsyncClient', Transport)
    client = GatewayClient(_config())
    await client._post({'phase': 'preflight'})
    await client._post({'phase': 'execute'})
    await client._post({'phase': 'result'})
    assert timeouts == [10, 300, 10]


@pytest.mark.asyncio
@pytest.mark.parametrize('diagnostic', ['', 'DOCUMENT_ARGUMENT_INVALID'])
async def test_guard_failure_persists_only_ack_metadata_and_flush_never_executes(tmp_path, monkeypatch, diagnostic):
    import json, stat
    client = GatewayClient(_config(), outbox=GatewayResultOutbox(tmp_path))
    preflight = {'tool_call_id':'runtime_call_001','permit':_permit({'query':'private input must not persist'})}
    attempts = []
    async def failed(payload):
        attempts.append(payload)
        raise GatewayError('transport unavailable')
    monkeypatch.setattr(client, '_post', failed)
    with pytest.raises(GatewayError, match='synchronization is pending'):
        await client.report_guard(preflight, 'block', validation_error_code=diagnostic)
    assert len(attempts) == 3
    pending = client.guard_outbox.pending('task_001')
    assert len(pending) == 1
    serialized = json.dumps(pending)
    assert 'private input' not in serialized and 'worker-secret' not in serialized
    paths = list((tmp_path / 'guard').glob('*.json'))
    assert stat.S_IMODE(paths[0].stat().st_mode) == 0o600
    async def ack(payload):
        assert payload['phase'] == 'guard'
        assert 'input' not in payload
        assert payload.get('validation_error_code', '') == diagnostic
        return {'tool_call_id':payload['tool_call_id'],'guard_decision':'block','status':'cancelled'}
    monkeypatch.setattr(client, '_post', ack)
    delivery = await client.guard_outbox.flush('task_001', client._send_guard)
    assert delivery['delivered'] == 1
    assert not client.guard_outbox.pending('task_001')


@pytest.mark.asyncio
async def test_refresh_reuses_fixed_call_and_input_hash(tmp_path, monkeypatch):
    client = GatewayClient(_config(), outbox=GatewayResultOutbox(tmp_path))
    body={'query':'public policy'}
    original={'tool_call_id':'runtime_call_001','permit':_permit(body),'call_id':'same-call'}
    async def refresh(payload):
        assert payload['phase']=='refresh'
        assert payload['tool_call_id']=='runtime_call_001'
        assert payload['permit_id']=='permit_001'
        assert payload['permit_nonce']=='nonce-once'
        assert payload['input_hash']==canonical_payload_hash(body)
        assert payload['idempotency_key']=='qwenpaw:same-call'
        fresh=_permit(body)
        fresh['payload']['permit_id']='fresh_permit'
        return {'phase':'allow','decision':'allow','status':'allowed','tool_call_id':'runtime_call_001','permit':fresh}
    monkeypatch.setattr(client,'_post',refresh)
    result=await client.preflight('policy_search',body,call_id='same-call',refresh=original)
    assert result['tool_call_id']=='runtime_call_001'
    assert result['permit']['payload']['permit_id']=='fresh_permit'

@pytest.mark.asyncio
async def test_guard_authorization_denial_is_preserved_without_retry_or_outbox(tmp_path, monkeypatch):
    from bank_runtime.presentation import failure_message
    client = GatewayClient(_config(), outbox=GatewayResultOutbox(tmp_path))
    preflight = {'tool_call_id': 'runtime_call_001', 'permit': _permit({'query': 'public'})}
    attempts = []
    async def denied(payload):
        attempts.append(payload)
        raise GatewayError('private identity must not reach model', code='FORBIDDEN',
                           failure_metadata={'execution_status': 'not_started_cancelled', 'retryable': False})
    monkeypatch.setattr(client, '_post', denied)
    with pytest.raises(GatewayError) as caught:
        await client.report_guard(preflight, 'allow')
    assert caught.value.code == 'FORBIDDEN'
    assert len(attempts) == 1
    assert not client.guard_outbox.pending('task_001')
    assert caught.value.execution_status == 'not_started_cancelled'
    assert caught.value.failure_metadata['recovery_action'] == 'stop'
    message = failure_message(caught.value.code)
    assert '授权' in message and '未执行' in message
    assert 'private identity' not in message

@pytest.mark.asyncio
async def test_guard_http_permanent_rejection_does_not_become_transport_failure(tmp_path, monkeypatch):
    import httpx
    attempts = []
    class Transport:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, *args, **kwargs):
            attempts.append(kwargs)
            return httpx.Response(403, json={'detail': {'code': 'FORBIDDEN', 'message': 'sensitive user identity',
                                  'details': {'execution_status': 'not_started_cancelled', 'retryable': False}}})
    monkeypatch.setattr(httpx, 'AsyncClient', Transport)
    client = GatewayClient(_config(), outbox=GatewayResultOutbox(tmp_path))
    with pytest.raises(GatewayError) as caught:
        await client.report_guard({'tool_call_id': 'runtime_call_001', 'permit': _permit({})}, 'allow')
    assert caught.value.code == 'FORBIDDEN'
    assert caught.value.http_status == 403
    assert len(attempts) == 1
    assert not client.guard_outbox.pending('task_001')
    assert 'sensitive user identity' not in str(caught.value)

@pytest.mark.asyncio
async def test_queued_guard_fixed_rejection_is_removed_and_preserved(tmp_path, monkeypatch):
    client = GatewayClient(_config(), outbox=GatewayResultOutbox(tmp_path))
    client.guard_outbox.enqueue_guard({'phase': 'guard', **client._scope_payload(),
                                     'tool_call_id': 'runtime_call_001', 'guard_decision': 'allow'})
    calls = []
    async def refused(payload):
        calls.append(payload['phase'])
        raise GatewayError('fixed refusal', code='FORBIDDEN', violation='user_authority_unverified')
    monkeypatch.setattr(client, '_post', refused)
    with pytest.raises(GatewayError) as caught:
        await client.preflight('policy_search', {})
    assert caught.value.code == 'FORBIDDEN'
    assert caught.value.violation == 'user_authority_unverified'
    assert calls == ['guard']
    assert not client.guard_outbox.pending('task_001')

@pytest.mark.asyncio
@pytest.mark.parametrize('status,code,attempts,queued', [(401, 'UNAUTHORIZED', 1, False),
    (422, 'INVALID_REQUEST', 1, False), (409, 'PERMIT_EXPIRED', 1, False),
    (410, 'PERMIT_EXPIRED', 1, False), (429, 'RATE_LIMITED', 3, True)])
async def test_guard_rejections_keep_native_code_and_transient_failures_retry(tmp_path, monkeypatch, status, code, attempts, queued):
    client = GatewayClient(_config(), outbox=GatewayResultOutbox(tmp_path))
    calls = []
    async def refused(payload):
        calls.append(payload)
        raise GatewayError('safe test rejection', code=code, http_status=status,
                           failure_metadata={'execution_status': 'not_started', 'retryable': False})
    monkeypatch.setattr(client, '_post', refused)
    with pytest.raises(GatewayError) as caught:
        await client.report_guard({'tool_call_id': 'runtime_call_001', 'permit': _permit({})}, 'allow')
    assert caught.value.code == ('TOOL_GUARD_REPORT_FAILED' if queued else code)
    assert len(calls) == attempts
    assert bool(client.guard_outbox.pending('task_001')) is queued

@pytest.mark.asyncio
async def test_non_json_forbidden_response_remains_fixed_denial(tmp_path, monkeypatch):
    import httpx
    class Transport:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, *args, **kwargs): return httpx.Response(403, text='private HTML identity error')
    monkeypatch.setattr(httpx, 'AsyncClient', Transport)
    client = GatewayClient(_config(), outbox=GatewayResultOutbox(tmp_path))
    with pytest.raises(GatewayError) as caught:
        await client.report_guard({'tool_call_id': 'runtime_call_001', 'permit': _permit({})}, 'allow')
    assert caught.value.code == 'FORBIDDEN'
    assert caught.value.http_status == 403
    assert not client.guard_outbox.pending('task_001')
    assert 'private HTML' not in str(caught.value)

@pytest.mark.asyncio
async def test_result_callback_forbidden_does_not_claim_tool_was_not_started(tmp_path, monkeypatch):
    import httpx
    class Transport:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, *args, **kwargs): return httpx.Response(403, json={'detail': {'code': 'FORBIDDEN'}})
    monkeypatch.setattr(httpx, 'AsyncClient', Transport)
    client = GatewayClient(_config(), outbox=GatewayResultOutbox(tmp_path))
    with pytest.raises(GatewayError) as caught:
        await client._post({'phase': 'result'})
    assert caught.value.execution_status == 'execution_unknown'
    assert 'not executed' not in str(caught.value)
    with pytest.raises(GatewayError) as guard:
        await client._post({'phase': 'guard'})
    assert guard.value.execution_status == 'not_started'


@pytest.mark.parametrize('decision, diagnostic', [('allow', 'DOCUMENT_ARGUMENT_INVALID'),
                                                 ('block', 'arbitrary_diagnostic')])
def test_guard_outbox_rejects_untrusted_validation_diagnostics(tmp_path, decision, diagnostic):
    client = GatewayClient(_config(), outbox=GatewayResultOutbox(tmp_path))
    with pytest.raises(ValueError, match='validation diagnostic'):
        client.guard_outbox.enqueue_guard({'phase': 'guard', **client._scope_payload(),
            'tool_call_id': 'runtime_call_001', 'guard_decision': decision,
            'validation_error_code': diagnostic})
    assert not client.guard_outbox.pending('task_001')
