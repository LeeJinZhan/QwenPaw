"""Temporary single-task diagnostics. Never used to make authorization decisions.

All recording is best effort, bounded, private and disabled without an expiring
operator control file. Values are explicitly projected; exception text and
business payloads must never be persisted here.
"""
import fcntl
import functools
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import time

ROOT = Path('/app/file-diagnostic')
_TASK = re.compile(r'task_[A-Za-z0-9_-]{1,100}\Z')
_grant_tasks = {}


def _read(name):
    fd = os.open(ROOT / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        import stat
        if not stat.S_ISREG(os.fstat(fd).st_mode): raise ValueError('not_regular')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            data = stream.read(4097)
        if len(data)>4096: raise ValueError('too_large')
        return json.loads(data)
    finally:
        os.close(fd)


def _write(name, data):
    fd = os.open(ROOT / name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        os.write(fd, json.dumps(data, separators=(',', ':')).encode())
    finally:
        os.close(fd)


def _ref(value, key):
    kind = 'missing' if value is None else 'string' if isinstance(value,str) else type(value).__name__
    if isinstance(value,str):
        kind = next((p for p in ('ds1_', 'dr1_', 'fr1_', 'file_', 'dc1_') if value.startswith(p)), 'other_string')
    # Oversized/unexpected objects are never stringified or traversed.
    encoded = value.encode() if isinstance(value,str) and len(value)<=32768 else str(value).encode() if type(value) in (int,bool) else b''
    return {'kind':kind, 'digest':hmac.new(key,encoded,hashlib.sha256).hexdigest()[:24] if encoded else None}


def _args(value,key):
    if not isinstance(value,dict): return {'kind':'not_object'}
    result={k:_ref(value.get(k),key) for k in ('document_ref','cursor','row_cursor','sheet') if k in value}
    for k in ('limit','include_header'):
        if k in value:
            result[k]={'type':type(value[k]).__name__}
            if type(value[k]) in (int,bool) and -100000<=value[k]<=100000: result[k]['value']=value[k]
    if 'format' in value: result['format']=value['format'] if value['format'] in ('markdown','records','source','inventory','cell') else 'other'
    if isinstance(value.get('documents'),list):
        result['documents']=[{k:_ref(item.get(k),key) for k in ('file_id','file_ref')} for item in value['documents'][:5] if isinstance(item,dict)]
    return result


def _result(value,key):
    if not isinstance(value,dict): return {'kind':'not_object'}
    result={}
    for k in ('status','content_mode','error_code'):
        v=value.get(k)
        if isinstance(v,str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,80}',v): result[k]=v
    for k in ('document_ref','next_cursor','next_inventory_cursor','next_row_cursor'):
        if k in value: result[k]=_ref(value[k],key)
    if isinstance(value.get('items'),list):
        result['items']=[{**_result({k:v for k,v in item.items() if k!='items'},key), 'file_id':_ref(item.get('file_id'),key)} for item in value['items'][:5] if isinstance(item,dict)]
    return result


def emit(task_id, stage, *, tool='', arguments=None, result=None, exc=None, **checks):
    """No exception from diagnostics may affect the observed business call."""
    try:
        if not isinstance(task_id,str) or not _TASK.fullmatch(task_id): return False
        if not re.fullmatch(r'[a-z_.]{1,64}',stage): return False
        if tool and tool.removeprefix('MinerU__') not in {'parse_documents','read_document_chunks','read_range','aggregate','search','analyze','file.resolve'}: return False
        control=_read('control.json')
        remaining=float(control['expires'])-time.time()
        if not 0<remaining<=900 or control['task_id'] not in ('next',task_id): return False
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',control['run']): return False
        key=bytes.fromhex(control['key'])
        if len(key)!=32: return False
        lock=os.open(ROOT/'.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_NONBLOCK,0o600)
        try:
            # A busy recorder drops an event instead of delaying execution.
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            try: state=_read('state.json')
            except FileNotFoundError: state={}
            if state.get('run')!=control['run']: state={'run':control['run'],'task_id':task_id,'count':0}
            if state['task_id']!=task_id or state['count']>=500: return False
            value={'event':'bank.file_diagnostic','run':control['run'],'task_id':task_id,'stage':stage,'tool':tool,'pid':os.getpid(),'time':round(time.time(),3),'seq':state['count']+1}
            if arguments is not None: value['arguments']=_args(arguments,key)
            if result is not None: value['result']=_result(result,key)
            value['checks']={k:v for k,v in checks.items() if re.fullmatch(r'[a-z_]{1,48}',k) and type(v) is bool}
            if exc is not None:
                code=getattr(exc,'code',getattr(exc,'error_code',''))
                value['error_code']=code if isinstance(code,str) and re.fullmatch(r'[A-Z_]{1,80}',code) else 'UNCLASSIFIED'
                value['exception_type']=type(exc).__name__[:80]
                frames=[]; cause=exc; seen=set()
                while cause is not None and id(cause) not in seen and len(seen)<4:
                    seen.add(id(cause)); tb=cause.__traceback__
                    while tb:
                        path=tb.tb_frame.f_code.co_filename
                        for package in ('bank_runtime/','bank_mineru_mcp/'):
                            if package in path:
                                relative=package+path.split(package,1)[1]
                                if re.fullmatch(r'[A-Za-z0-9_/.-]{1,180}',relative): frames.append({'module':relative,'line':tb.tb_lineno})
                                break
                        tb=tb.tb_next
                    cause=cause.__cause__
                value['frames']=frames[-12:]
            encoded=(json.dumps(value,separators=(',',':'))+'\n').encode()
            if len(encoded)>12000: return False
            fd=os.open(ROOT/'events.jsonl',os.O_WRONLY|os.O_CREAT|os.O_APPEND|os.O_NOFOLLOW|os.O_NONBLOCK,0o600)
            try:
                import stat
                info=os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_size+len(encoded)>1024*1024: return False
                state['count']+=1
                _write('state.json',state)
                os.write(fd,encoded)
            finally: os.close(fd)
            return True
        finally: os.close(lock)
    except Exception:
        return False


def grant_issued(task_id, nonce, name, arguments):
    try:
        if emit(task_id,'grant.issued',tool=name,arguments=arguments):
            now=time.monotonic()
            for token,(_,expiry) in list(_grant_tasks.items()):
                if expiry<now: _grant_tasks.pop(token,None)
            if len(_grant_tasks)<256: _grant_tasks[nonce]=(task_id,now+900)
    except Exception: pass


def task_for_metadata(metadata):
    try:
        if hasattr(metadata,'model_dump'): metadata=metadata.model_dump()
        item=_grant_tasks.get(metadata.get('bank_runtime_document_grant'))
        return item[0] if item and item[1]>time.monotonic() else ''
    except Exception: return ''


def grant_consumed(metadata, grant, name, arguments):
    try:
        from .document_access import _digest
        task=grant[0] if grant else task_for_metadata(metadata)
        emit(task,'grant.consume',tool=name,arguments=arguments,grant_found=bool(grant),
             name_matches=bool(grant and grant[1]==name),
             arguments_match=bool(grant and grant[2]==_digest(name,arguments)),
             grant_live=bool(grant and grant[3]>time.monotonic()))
    except Exception: pass


def mcp_call(function):
    @functools.wraps(function)
    async def wrapped(self,name,arguments):
        task=''
        try: task=task_for_metadata(self.mcp.get_context().request_context.meta)
        except Exception: pass
        emit(task,'mcp.input',tool=name,arguments=arguments)
        try: result=await function(self,name,arguments)
        except BaseException as exc:
            emit(task,'mcp.error',tool=name,exc=exc)
            raise
        emit(task,'mcp.result',tool=name,result=result)
        return result
    return wrapped


def source_call(function):
    @functools.wraps(function)
    def wrapped(self,reference):
        task=''
        try: task=self._current_task()
        except Exception: pass
        emit(task,'source.input',tool='file.resolve',arguments={'documents':[{'file_ref':reference}]})
        try: result=function(self,reference)
        except BaseException as exc:
            emit(task,'source.error',tool='file.resolve',exc=exc)
            raise
        emit(task,'source.result',tool='file.resolve',task_matches=getattr(result,'task_id',None)==task)
        return result
    return wrapped


def ledger(task_id, name, arguments, documents):
    try:
        emit(task_id, 'gateway.ledger', tool=name, arguments=arguments,
             ledger_match=arguments.get('document_ref') in documents)
    except Exception: pass
