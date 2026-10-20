"""Unavailable source bytes must not be reported as a model failure."""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bank_runtime.sandbox.broker import RuntimeFileBroker
from bank_runtime.artifact_tools import ArtifactDeliveryErrorHook
from bank_runtime.events import CompactEventProjector


@pytest.mark.asyncio
@pytest.mark.parametrize('provider, storage_error', [
    ('local', 'NoSuchBucket'), ('oss', 'NoSuchBucket'),
    ('oss', 'NoSuchKey'), ('oss', 'AccessDenied'),
])
async def test_source_storage_failure_remains_safe_through_error_projection(provider, storage_error, tmp_path, monkeypatch):
    import oss2
    monkeypatch.setenv('QWENPAW_LOCAL_OBJECT_ROOT', str(tmp_path))
    monkeypatch.setenv('OSS_ENDPOINT', 'https://storage.invalid')
    monkeypatch.setenv('OSS_ACCESS_KEY_ID', 'test-key')
    monkeypatch.setenv('OSS_ACCESS_KEY_SECRET', 'test-secret')
    source_error = getattr(oss2.exceptions, storage_error)(
        403 if storage_error == 'AccessDenied' else 404, {}, b'', {
            'Code': storage_error, 'Message': 'private storage detail'})
    def missing_object(*args):
        raise source_error
    monkeypatch.setattr(oss2.Bucket, 'get_object', missing_object)
    broker = RuntimeFileBroker('http://runtime.invalid', 'test-token')
    with pytest.raises(Exception) as caught:
        broker.stream_locator({'storage_provider': provider, 'bucket': 'missing-bucket',
                               'object_key': 'uploads/missing.doc'}, lambda chunk: None)
    error = caught.value
    assert error.error_code == 'FILE_ACCESS_DENIED'
    assert error.__cause__ is not None
    assert 'private storage detail' not in str(error)
    assert 'missing-bucket' not in str(error)
    from qwenpaw.hooks.error.error_hook import ErrorNormalizeHook
    from qwenpaw.app.chats import query_error_dump
    monkeypatch.setattr(query_error_dump, 'write_query_error_dump', lambda *args: None)
    ctx = SimpleNamespace(error=error, extras={}, agent=None, request=None)
    await ErrorNormalizeHook().run(ctx)
    await ArtifactDeliveryErrorHook().run(ctx)
    assert ctx.extras['_error_code'] == 'FILE_ACCESS_DENIED'
    assert '模型' not in ctx.extras['_error_text']
    events = CompactEventProjector('task_test').project({'event': 'error', 'error': {
        'code': ctx.extras['_error_code'], 'message': ctx.extras['_error_text']}})
    assert events[0]['error_code'] == 'FILE_ACCESS_DENIED'
    assert 'private storage detail' not in str(events)
