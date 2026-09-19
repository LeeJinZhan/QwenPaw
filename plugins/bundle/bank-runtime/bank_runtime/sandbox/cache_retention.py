"""Reclaim marked, expired task caches while honoring cross-process file leases."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import asyncio
import fcntl
import json
import logging
import os
import re
import shutil

_MARKER = '.runtime-cache.json'
_LOCK = '.runtime-cache.lock'
_TASK = re.compile(r'[A-Za-z0-9_-]{1,160}')


class CacheLease:
    def __init__(self, task_root):
        root = Path(task_root)
        self.fd = os.open(root / _LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(self.fd)
            raise RuntimeError('Task cache is in use') from exc

    def mark(self, task_root, scope):
        expiry = str(scope.sandbox_context.get('expires_at') or '')
        try:
            parsed = datetime.fromisoformat(expiry.replace('Z', '+00:00'))
            if parsed.tzinfo is None:
                return
        except ValueError:
            return  # Unmarked legacy caches are never guessed to be safe to delete.
        target = Path(task_root) / _MARKER
        value = json.dumps({'task_id': scope.task_id, 'expires_at': expiry,
                            'created_at': datetime.now(timezone.utc).isoformat()})
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(value)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


def sweep_expired_caches(root, *, ttl_seconds=86400, now=None):
    root = Path(root)
    result = {'cleaned': 0, 'failed': 0}
    if not root.is_dir() or root.is_symlink():
        return result
    now = now or datetime.now(timezone.utc)
    # Stream directory entries; never load file contents or unbounded result lists.
    with os.scandir(root) as entries:
        for entry in entries:
            if not entry.is_dir(follow_symlinks=False) or not _TASK.fullmatch(entry.name):
                continue
            task_root = Path(entry.path)
            marker = task_root / _MARKER
            lease = None
            try:
                if marker.is_symlink() or not marker.is_file() or marker.stat().st_size > 2048:
                    continue
                before = task_root.stat()
                lease = CacheLease(task_root)
                after = task_root.stat()
                if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                    continue
                value = json.loads(marker.read_text())
                if not isinstance(value, dict) or value.get('task_id') != entry.name:
                    continue
                created = datetime.fromisoformat(value['created_at'].replace('Z', '+00:00'))
                expiry = datetime.fromisoformat(value['expires_at'].replace('Z', '+00:00'))
                if created.tzinfo is None or expiry.tzinfo is None:
                    continue
                if created + timedelta(seconds=ttl_seconds) > now or expiry > now:
                    continue
                shutil.rmtree(task_root)
                result['cleaned'] += 1
            except RuntimeError:
                continue  # Another Runtime-managed request still holds its cache lease.
            except (ValueError, KeyError, TypeError):
                continue  # Invalid markers are not authority to delete.
            except OSError:
                result['failed'] += 1
            finally:
                if lease is not None:
                    lease.close()
    return result


class CacheCleanupLoop:
    def __init__(self, root):
        self.root = root
        self.task = None
        self.stopping = asyncio.Event()

    async def start(self):
        self.interval = max(5, int(os.environ.get('QWENPAW_TASK_CACHE_CLEANUP_INTERVAL_SECONDS', '300')))
        self.ttl = max(3600, int(os.environ.get('QWENPAW_TASK_FILE_TTL_SECONDS', '86400')))
        self.stopping.clear()
        self.task = asyncio.create_task(self._run())

    async def _run(self):
        logger = logging.getLogger(__name__)
        while not self.stopping.is_set():
            try:
                result = await asyncio.to_thread(sweep_expired_caches, self.root, ttl_seconds=self.ttl)
                if result['failed']:
                    logger.warning('task_cache.cleanup_failed count=%s', result['failed'])
            except Exception as exc:
                logger.error('task_cache.cleanup_failed error_type=%s', type(exc).__name__)
            try:
                await asyncio.wait_for(self.stopping.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass

    async def close(self):
        self.stopping.set()
        if self.task is not None:
            await self.task
            self.task = None
