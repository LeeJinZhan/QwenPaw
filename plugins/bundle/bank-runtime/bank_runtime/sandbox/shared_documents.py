"""Final document text is published after OCR and queried with durable cursors."""
import asyncio
from dataclasses import asdict
import json
import re
import httpx
from .executor import SandboxExecutorError,_remaining_seconds

DOCUMENT_REF=re.compile(r'dr2_[a-f0-9]{32}_[a-f0-9]{64}_[a-f0-9]{64}')


async def request(execution,action,**extra):
    if action not in {'publish','read'}:raise SandboxExecutorError('Invalid document operation')
    executor=execution.executor
    remaining=_remaining_seconds(executor.sandbox_context)
    payload=dict(sandbox_context=executor.sandbox_context,tool_call_id=execution.tool_call_id,
        arguments=execution.arguments,**extra)
    encoded=json.dumps(payload,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()
    if len(encoded)>32*1024**2+131072:raise SandboxExecutorError('Document exceeds publication limit','DOCUMENT_RESULT_TOO_LARGE')
    try:
        async with asyncio.timeout(remaining),httpx.AsyncClient(trust_env=False,follow_redirects=False,
                timeout=httpx.Timeout(remaining,connect=min(5,remaining))) as client:
            async with client.stream('POST',executor.base_url+'/runtime/internal/sandbox/documents/'+action,
                    content=encoded,headers={'Authorization':f'Bearer {executor.token}','Content-Type':'application/json'}) as response:
                if response.status_code in {401,403,404}:raise SandboxExecutorError('Document unavailable','FILE_ACCESS_DENIED')
                if response.status_code>=400:raise SandboxExecutorError('Document storage unavailable')
                content=bytearray()
                async for block in response.aiter_bytes(8192):
                    if len(content)+len(block)>65536:raise ValueError('Document response exceeds bound')
                    content.extend(block)
        value=json.loads(content)['data']
        if not isinstance(value,dict):raise ValueError('Invalid document response')
        return value
    except (ValueError,KeyError,TypeError,httpx.HTTPError,TimeoutError) as exc:
        raise SandboxExecutorError('Document storage unavailable') from exc


async def publish(execution,file_ref,document):
    value=asdict(document)
    value.pop('error_code',None)
    result=await request(execution,'publish',file_ref=file_ref,document=value)
    if (set(result)!={'document_ref','chunk_count'} or not isinstance(result['document_ref'],str)
            or not DOCUMENT_REF.fullmatch(result['document_ref']) or type(result['chunk_count']) is not int
            or result['chunk_count']!=len(document.chunks)):
        raise SandboxExecutorError('Invalid document publication receipt')
    return result['document_ref']


async def read(execution):
    result=await request(execution,'read')
    if (result.get('document_ref')!=execution.arguments.get('document_ref')
            or set(result)!={'document_ref','chunks','next_cursor','has_more','coverage','recovery_hint'}
            or not isinstance(result['chunks'],list) or len(result['chunks'])>execution.arguments.get('limit',5)
            or type(result['has_more']) is not bool
            or result['next_cursor'] is not None and (not isinstance(result['next_cursor'],str)
                or not re.fullmatch(r'cur2_[0-9]{1,5}_[a-f0-9]{64}',result['next_cursor']))):
        raise SandboxExecutorError('Invalid document page receipt')
    return result
