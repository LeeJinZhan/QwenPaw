"""Bounded fixed-endpoint transport for recoverable task file references."""
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import re
import time
import httpx

REFERENCE = re.compile(r'fr2_[a-f0-9]{2,320}_[a-f0-9]{64}_[a-f0-9]{64}')
PREFIX = '/runtime/internal/sandbox/file-references'


async def metadata(broker, context, action, **values):
    from .broker import SandboxBrokerError
    from .executor import _remaining_seconds
    if action not in {'issue','resolve'}:raise SandboxBrokerError('Invalid reference operation')
    remaining=_remaining_seconds(context)
    try:
        async with asyncio.timeout(remaining), httpx.AsyncClient(trust_env=False,follow_redirects=False,
                timeout=httpx.Timeout(min(30,remaining),connect=min(5,remaining))) as client:
            async with client.stream('POST',broker.base_url+PREFIX+'/'+action,
                    json=dict(sandbox_context=context,**values),headers={'Authorization':f'Bearer {broker.token}'}) as response:
                if response.status_code>=400:raise SandboxBrokerError('File reference unavailable')
                content=bytearray()
                async for block in response.aiter_bytes(8192):
                    if len(content)+len(block)>65536:raise SandboxBrokerError('Reference response exceeds limit')
                    content.extend(block)
        value=json.loads(content)['data']
        if not isinstance(value,dict):raise ValueError()
        return value
    except (ValueError,KeyError,TypeError,httpx.HTTPError,TimeoutError) as exc:
        raise SandboxBrokerError('File reference unavailable') from exc


async def issue(broker,context,files):
    from .broker import SandboxBrokerError
    values=await metadata(broker,context,'issue',files=files)
    refs=values.get('files')
    if (set(values)!={'files'} or not isinstance(refs,list) or len(refs)!=len(files)
            or any(not isinstance(item,dict) or set(item)!={'file_id','file_ref'}
                or not isinstance(item['file_ref'],str) or not REFERENCE.fullmatch(item['file_ref']) for item in refs)):
        raise SandboxBrokerError('Invalid shared file references')
    result={item['file_id']:item['file_ref'] for item in refs}
    if len(result)!=len(refs) or set(result)!={item['file_id'] for item in files}:
        raise SandboxBrokerError('Shared file reference set changed')
    return result


async def resolve(broker,context,file_ref,file_id):
    from .broker import SandboxBrokerError
    from .executor import _remaining_seconds
    if not isinstance(file_ref,str) or not REFERENCE.fullmatch(file_ref):raise SandboxBrokerError('Invalid file reference')
    value=await metadata(broker,context,'resolve',file_ref=file_ref)
    try:
        if (set(value)!={'file_ref','file_id','content_hash','size_bytes','extension','content_type','original_name','expires_at'}
                or value['file_ref']!=file_ref or value['file_id']!=file_id
                or not re.fullmatch('[A-Za-z0-9_-]{1,160}',file_id)
                or not isinstance(value['content_hash'],str) or not re.fullmatch('[a-f0-9]{64}',value['content_hash'])
                or type(value['size_bytes']) is not int or not 0<=value['size_bytes']<=200*1024**2
                or not isinstance(value['extension'],str) or not re.fullmatch(r'\.[a-z0-9]{1,15}',value['extension'])
                or not isinstance(value['original_name'],str) or len(value['original_name'])>1024
                or not isinstance(value['content_type'],str) or len(value['content_type'])>128):raise ValueError()
        expiry=datetime.fromisoformat(value['expires_at'].replace('Z','+00:00'))
        if expiry.tzinfo is None or not datetime.now(timezone.utc)<expiry<=datetime.fromisoformat(context['expires_at'].replace('Z','+00:00')):
            raise ValueError()
        _remaining_seconds(context)
    except (ValueError,KeyError,TypeError,AttributeError) as exc:
        raise SandboxBrokerError('Invalid file reference metadata') from exc
    return value


def stream(broker,locator,write_chunk):
    from .broker import SandboxBrokerError
    from .executor import _remaining_seconds
    context=locator['_sandbox_context']
    remaining=_remaining_seconds(context)
    deadline=time.monotonic()+remaining
    total,digest=0,hashlib.sha256()
    with httpx.Client(trust_env=False,follow_redirects=False,
            timeout=httpx.Timeout(min(30,remaining),connect=min(5,remaining))) as client:
        with client.stream('POST',broker.base_url+PREFIX+'/read',
                json=dict(sandbox_context=context,file_ref=locator['file_ref']),
                headers={'Authorization':f'Bearer {broker.token}'}) as response:
            if (response.status_code!=200 or response.headers.get('X-Content-SHA256')!=locator['content_hash']
                    or response.headers.get('Content-Length')!=str(locator['size_bytes'])):
                raise SandboxBrokerError('Shared file unavailable')
            for block in response.iter_bytes(65536):
                total+=len(block)
                if total>locator['size_bytes'] or time.monotonic()>=deadline:raise SandboxBrokerError('Shared file limit exceeded')
                digest.update(block)
                write_chunk(block)
    if total!=locator['size_bytes'] or digest.hexdigest()!=locator['content_hash']:
        raise SandboxBrokerError('Shared file integrity changed')


async def restore(executor,file_ref,file_id,registry):
    from .broker import RuntimeFileBroker,SandboxBrokerError
    from .cache import get_task_attachment_cache
    from .scope import SandboxRequestScope
    broker=RuntimeFileBroker(executor.base_url,executor.token)
    expected=await resolve(broker,executor.sandbox_context,file_ref,file_id)
    scope=SandboxRequestScope(task_id=executor.sandbox_context['task_id'],sandbox_context=executor.sandbox_context,
        attachments_manifest=(),current_attachment_ids=())
    cache=get_task_attachment_cache()
    prepared=(await cache.prepare_files(scope,[file_id],broker))[0]
    if prepared.sha256!=expected['content_hash'] or prepared.size_bytes!=expected['size_bytes']:
        raise SandboxBrokerError('Restored file differs from reference')
    await resolve(broker,executor.sandbox_context,file_ref,file_id)
    registry.bind_shared_reference(prepared,file_ref,expires_at=datetime.fromisoformat(expected['expires_at'].replace('Z','+00:00')))
    return registry.resolve(file_ref,expected_task_id=scope.task_id)
