"""Task-scoped durable parse jobs with cross-process admission and cancellation.

The Runtime-authorized MCP call owns execution. Only private, fixed module
entry points run in children; file authorization stays in the parent. The job
lock is inherited by the child, preventing a retry racing an orphaned writer.
"""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import shutil
import signal
import sys
import time

from .structured_store import StructuredStoreError


@asynccontextmanager
async def file_lock(path):
    with path.open('a+b') as stream:
        while True:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                await asyncio.sleep(.1)
        try:
            yield stream.fileno()
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def write_state(path, status, **fields):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps({'status': status, 'updated_at': datetime.now(timezone.utc).isoformat(), **fields}), encoding='utf-8')
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def source_nonce(store, source):
    digest = hashlib.sha256()
    with source.path.open('rb') as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    identity = json.dumps([source.task_id, getattr(source, 'file_id', source.path.name), digest.hexdigest(), 'reading-2'], separators=(',', ':')).encode()
    return hmac.new(store.key, identity, hashlib.sha256).digest()


async def parse_job(store, source, *, timeout=900, memory_bytes=4 * 1024**3, progress=None):
    nonce = await asyncio.to_thread(source_nonce, store, source)
    identifier = hashlib.sha256(nonce).hexdigest()
    task = (store.root / source.task_id).resolve(strict=True)
    if task.parent != store.root or (store.root / source.task_id).is_symlink():
        raise StructuredStoreError('FILE_ACCESS_DENIED', 'Invalid task scope')
    jobs = task / '.reading-jobs'
    jobs.mkdir(mode=0o700, exist_ok=True)
    if jobs.is_symlink():
        raise StructuredStoreError('FILE_ACCESS_DENIED', 'Invalid job scope')
    state = jobs / (identifier + '.json')
    work = jobs / (identifier + '.work')
    result_path = jobs / (identifier + '.result')
    process = None
    started = time.monotonic()
    async with asyncio.timeout(timeout):
        async with file_lock(jobs / (identifier + '.lock')) as job_fd:
            cached = store.cached(nonce, source.task_id)
            if cached is not None:
                write_state(state, 'ready', document_ref=cached.document_ref, reused=True)
                return cached, store.inventory(cached.document_ref)
            # Recover a child that finished extraction after its parent died.
            saved = None
            if result_path.is_file() and work.is_dir():
                saved = json.loads(result_path.read_text())
            try:
                if not saved or saved.get('status') != 'ready':
                    shutil.rmtree(work, ignore_errors=True)
                    result_path.unlink(missing_ok=True)
                    write_state(state, 'queued')
                    # One heavy extraction per shared worker root. Queries and
                    # normal chat do not hold this slot.
                    async with file_lock(store.root / '.reading-slot.lock') as slot_fd:
                        if shutil.disk_usage(task).free < 512 * 1024**2:
                            raise StructuredStoreError('DOCUMENT_RESULT_TOO_LARGE', 'Insufficient extraction workspace')
                        write_state(state, 'running')
                        package_root = str(Path(__file__).resolve().parents[1])
                        environment = {**os.environ, 'PYTHONPATH': package_root + os.pathsep + os.environ.get('PYTHONPATH', '')}
                        process = await asyncio.create_subprocess_exec(sys.executable, '-m', 'bank_mineru_mcp.extract_process',
                            str(source.path), str(work), str(store.max_document_bytes), str(memory_bytes), str(result_path),
                            env=environment, pass_fds=(job_fd, slot_fd), start_new_session=True,
                            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                        while process.returncode is None:
                            try:
                                await asyncio.wait_for(process.wait(), timeout=2)
                            except TimeoutError:
                                temporary_bytes = sum(p.stat().st_size for p in work.rglob("*") if p.is_file())
                                if temporary_bytes > store.max_document_bytes * 2 or shutil.disk_usage(task).free < 256 * 1024**2:
                                    raise StructuredStoreError("DOCUMENT_RESULT_TOO_LARGE", "Extraction workspace quota exceeded")
                                if progress is not None:
                                    await progress(time.monotonic() - started)
                        if process.returncode != 0 or not result_path.is_file():
                            raise StructuredStoreError('DOCUMENT_PARSE_FAILED', 'Extraction process failed')
                        saved = json.loads(result_path.read_text())
                if saved.get('status') != 'ready':
                    raise StructuredStoreError(saved.get('error_code', 'DOCUMENT_PARSE_FAILED'), 'Extraction failed')
                handle = store.write(source, saved['inventory'], work, nonce=nonce)
                write_state(state, 'ready', document_ref=handle.document_ref)
                return handle, saved['inventory']
            except BaseException as exc:
                if process is not None and process.returncode is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    await process.wait()
                write_state(state, 'cancelled' if isinstance(exc, asyncio.CancelledError) else 'failed',
                            error_code=getattr(exc, 'code', 'DOCUMENT_PARSE_FAILED'))
                raise
            finally:
                shutil.rmtree(work, ignore_errors=True)
                result_path.unlink(missing_ok=True)


async def query_job(store, name, arguments, *, timeout=300, memory_bytes=4 * 1024**3):
    """Execute only a validated structured query in a cancellable process."""
    import tempfile
    process = None
    entry, _ = store._entry(arguments['document_ref'])
    with tempfile.TemporaryDirectory(prefix='.reading-query-', dir=entry.path) as directory:
        directory = Path(directory)
        request, response = directory/'request.json', directory/'response.json'
        request.write_text(json.dumps({'root':str(store.root),'key':store.key.hex(),
            'max_task_bytes':min(store.max_task_bytes, store.max_document_bytes),
            'max_document_bytes':store.max_document_bytes,'memory_bytes':memory_bytes,
            'page_chars':store.page_chars,'max_groups':store.max_groups,
            'name':'read_chunks' if name=='read_document_chunks' else name,'arguments':arguments}),encoding='utf-8')
        os.chmod(request,0o600)
        env={**os.environ,'SQLITE_TMPDIR':str(directory),'PYTHONPATH':str(Path(__file__).resolve().parents[1])+os.pathsep+os.environ.get('PYTHONPATH','')}
        try:
            async with asyncio.timeout(timeout):
                # One query can execute independently while a workbook parses.
                async with file_lock(store.root/'.reading-query-slot.lock') as slot_fd:
                    process = await asyncio.create_subprocess_exec(sys.executable,'-m','bank_mineru_mcp.query_process',str(request),str(response),
                        env=env,pass_fds=(slot_fd,),start_new_session=True,stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL)
                    while process.returncode is None:
                        try:
                            await asyncio.wait_for(process.wait(), timeout=2)
                        except TimeoutError:
                            query_bytes = sum(p.stat().st_size for p in entry.path.rglob('*')
                                              if p.is_file() and any(part.startswith(('.aggregate-', '.reading-query-')) for part in p.parts))
                            if query_bytes > store.max_document_bytes * 2 or shutil.disk_usage(entry.path).free < 256 * 1024**2:
                                raise StructuredStoreError('DOCUMENT_RESULT_TOO_LARGE', 'Query workspace quota exceeded')
                if process.returncode != 0 or not response.is_file():
                    raise StructuredStoreError('DOCUMENT_PARSE_FAILED','Query process failed')
                data=json.loads(response.read_text())
                if data['status']!='completed':
                    raise StructuredStoreError(data['error_code'],'Query failed')
                return data['result']
        finally:
            if process is not None and process.returncode is None:
                try:
                    os.killpg(process.pid,signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()
