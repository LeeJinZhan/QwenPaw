from datetime import datetime, timedelta, timezone
import json
import os
import pytest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bank_runtime.sandbox.cache_retention import CacheLease, sweep_expired_caches


def marker(root, task='task_old', *, age=90000, expires=-10):
    path=root/task; path.mkdir()
    (path/'content.docx').write_bytes(b'test bytes')
    now=datetime.now(timezone.utc)
    (path/'.runtime-cache.json').write_text(json.dumps({'task_id':task,
        'created_at':(now-timedelta(seconds=age)).isoformat(),
        'expires_at':(now+timedelta(seconds=expires)).isoformat()}))
    return path


def test_expired_inactive_cache_is_removed_and_unmarked_directory_preserved(tmp_path):
    old=marker(tmp_path); unknown=tmp_path/'task_unknown'; unknown.mkdir()
    (unknown/'valuable').write_text('keep')
    assert sweep_expired_caches(tmp_path,ttl_seconds=86400)['cleaned']==1
    assert not old.exists() and unknown.exists()


def test_active_cache_is_locked_against_another_cleaner(tmp_path):
    old=marker(tmp_path)
    lease=CacheLease(old)
    try:
        assert sweep_expired_caches(tmp_path,ttl_seconds=86400)['cleaned']==0
        assert old.exists()
    finally: lease.close()
    assert sweep_expired_caches(tmp_path,ttl_seconds=86400)['cleaned']==1


@pytest.mark.parametrize('age,expires',[(100,-10),(90000,3600)])
def test_age_and_scope_expiry_are_both_required(tmp_path,age,expires):
    old=marker(tmp_path,age=age,expires=expires)
    assert sweep_expired_caches(tmp_path,ttl_seconds=86400)['cleaned']==0
    assert old.exists()


def test_symlink_and_invalid_marker_are_not_followed(tmp_path):
    root=tmp_path/'cache'; root.mkdir(); outside=marker(tmp_path,'task_outside')
    (root/'task_outside').symlink_to(outside,target_is_directory=True)
    bad=marker(root,'task_bad'); (bad/'.runtime-cache.json').write_text('{')
    assert sweep_expired_caches(root,ttl_seconds=86400)['cleaned']==0
    assert (outside/'content.docx').exists() and bad.exists()


def test_same_task_cannot_acquire_live_cache_twice(tmp_path):
    old=marker(tmp_path); lease=CacheLease(old)
    try:
        with pytest.raises(RuntimeError): CacheLease(old)
    finally: lease.close()


@pytest.mark.asyncio
async def test_task_cache_lease_covers_preparation_until_final_cleanup(tmp_path):
    import hashlib
    from types import SimpleNamespace
    from bank_runtime.sandbox.cache import TaskAttachmentCache
    content=b"synthetic"
    class Broker:
        async def authorize_files(self,*a,**kw):
            return {"authorized":[{"file_id":"file_a","original_name":"file.txt","content_type":"text/plain",
                "size_bytes":len(content),"content_hash":hashlib.sha256(content).hexdigest()}],"denied":[]}
        def stream_locator(self,locator,write): write(content)
    scope=SimpleNamespace(task_id="task_a",sandbox_context={"expires_at":"2026-01-01T00:00:00+00:00"})
    cache=TaskAttachmentCache(root=tmp_path)
    prepared=await cache.prepare_files(scope,["file_a"],Broker())
    assert (tmp_path/"task_a"/".runtime-cache.json").exists()
    future=datetime.now(timezone.utc)+timedelta(days=2)
    assert sweep_expired_caches(tmp_path,now=future)["cleaned"]==0
    other=TaskAttachmentCache(root=tmp_path)
    with pytest.raises(RuntimeError): await other.cleanup("task_a")
    assert prepared[0].local_path.exists()
    await cache.cleanup("task_a")
    assert not (tmp_path/"task_a").exists()
