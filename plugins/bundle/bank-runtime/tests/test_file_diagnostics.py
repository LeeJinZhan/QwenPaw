import json
import time
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from bank_runtime.gateway import file_diagnostics as diag


def arm(root, target='next'):
    root.mkdir(exist_ok=True)
    (root / 'control.json').write_text(json.dumps({'run':'test-run', 'task_id':target,
        'expires':time.time()+900, 'key':'12'*32}))


def test_disabled_and_task_scoped_redacted_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(diag, 'ROOT', tmp_path)
    diag.emit('task_a', 'input', arguments={'document_ref':'secret'})
    assert not (tmp_path/'events.jsonl').exists()
    arm(tmp_path)
    for _ in range(510):
        diag.emit('task_a', 'input', arguments={'document_ref':'secret', 'query':'PRIVATE BODY'},
                  result={'status':'completed','items':[{'file_id':'file_private','document_ref':'secret','markdown':'PRIVATE BODY','content_mode':'chunked'}]})
    diag.emit('task_b','input')
    lines=(tmp_path/'events.jsonl').read_text().splitlines()
    assert len(lines)==500
    assert all(json.loads(x)['task_id']=='task_a' for x in lines)
    assert 'secret' not in '\n'.join(lines) and 'PRIVATE BODY' not in '\n'.join(lines) and 'file_private' not in '\n'.join(lines)
    first=json.loads(lines[0])
    assert first['arguments']['document_ref']['digest']==first['result']['items'][0]['document_ref']['digest']


def test_expiry_and_failure_do_not_change_calls(tmp_path, monkeypatch):
    monkeypatch.setattr(diag,'ROOT',tmp_path)
    arm(tmp_path)
    control=json.loads((tmp_path/'control.json').read_text()); control['expires']=time.time()-1
    (tmp_path/'control.json').write_text(json.dumps(control))
    diag.emit('task_a','input')
    assert not (tmp_path/'events.jsonl').exists()
    monkeypatch.setattr(diag,'ROOT',tmp_path/'missing'/'missing')
    diag.emit('task_a','input')


def test_source_wrapper_preserves_result_and_exception(tmp_path, monkeypatch):
    monkeypatch.setattr(diag,'ROOT',tmp_path); arm(tmp_path)
    class Source:
        task_id='task_a'
    class Service:
        def _current_task(self): return 'task_a'
        @diag.source_call
        def read(self, ref):
            if ref=='bad': raise ValueError('PRIVATE BODY')
            return source
    source=Source()
    assert Service().read('secret') is source
    with pytest.raises(ValueError,match='PRIVATE BODY'): Service().read('bad')
    assert 'PRIVATE BODY' not in (tmp_path/'events.jsonl').read_text()


def test_grants_expose_comparison_without_changing_authorization(tmp_path,monkeypatch):
    from bank_runtime.gateway.document_access import approved_document_grant,consume_document_call,DocumentAccessError
    monkeypatch.setattr(diag,'ROOT',tmp_path); arm(tmp_path)
    args={'document_ref':'dr1_private'}
    with approved_document_grant('task_a','read_document_chunks',args) as metadata:
        with pytest.raises(DocumentAccessError):
            with consume_document_call(metadata,'read_document_chunks',{'document_ref':'dr1_other'}): pass
        # Original single-use rejection behavior is retained.
        with pytest.raises(DocumentAccessError):
            with consume_document_call(metadata,'read_document_chunks',args): pass
    events=[json.loads(s) for s in (tmp_path/'events.jsonl').read_text().splitlines()]
    consumes=[e for e in events if e['stage']=='grant.consume']
    assert consumes[0]['checks']['grant_found'] is True
    assert consumes[0]['checks']['arguments_match'] is False
    assert consumes[1]['checks']['grant_found'] is False
    assert 'dr1_private' not in json.dumps(events)


@pytest.mark.asyncio
async def test_mcp_success_error_and_cancel_are_unchanged(tmp_path,monkeypatch):
    import asyncio
    from types import SimpleNamespace
    monkeypatch.setattr(diag,'ROOT',tmp_path); arm(tmp_path)
    diag.grant_issued('task_a','nonce','read_document_chunks',{'document_ref':'secret'})
    context=SimpleNamespace(request_context=SimpleNamespace(meta={'bank_runtime_document_grant':'nonce'}))
    result={'status':'completed','document_ref':'secret','markdown':'PRIVATE BODY'}
    class Service:
        mcp=SimpleNamespace(get_context=lambda:context)
        @diag.mcp_call
        async def call(self,name,arguments):
            if arguments.get('cancel'): raise asyncio.CancelledError()
            if arguments.get('error'): raise ValueError('PRIVATE BODY')
            return result
    assert await Service().call('read_document_chunks',{}) is result
    with pytest.raises(ValueError): await Service().call('read_document_chunks',{'error':True})
    with pytest.raises(asyncio.CancelledError): await Service().call('read_document_chunks',{'cancel':True})
    assert 'PRIVATE BODY' not in (tmp_path/'events.jsonl').read_text()


def test_multi_process_claim_and_symlink_failure(tmp_path,monkeypatch):
    monkeypatch.setattr(diag,'ROOT',tmp_path); arm(tmp_path)
    assert diag.emit('task_a','input')
    # Claim stored on disk, not a per-process variable.
    assert not diag.emit('task_b','input')
    outside=tmp_path/'outside'; outside.write_text('unchanged')
    (tmp_path/'events.jsonl').unlink(); (tmp_path/'events.jsonl').symlink_to(outside)
    assert not diag.emit('task_a','input')
    assert outside.read_text()=='unchanged'
