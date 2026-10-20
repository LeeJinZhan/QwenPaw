"""Private bounded-query subprocess; authorization is performed by its parent."""
import json
import os
from pathlib import Path
import resource
import sys
from .structured_store import StructuredStore, StructuredStoreError
from .inventory import inventory_page


def main():
    request_path, response_path = map(Path, sys.argv[1:])
    request = json.loads(request_path.read_text())
    if sys.platform == 'linux':
        resource.setrlimit(resource.RLIMIT_AS, (request['memory_bytes'], request['memory_bytes']))
    os.umask(0o077)
    store = StructuredStore(root=request['root'], process_start_key=bytes.fromhex(request['key']),
        max_task_bytes=request['max_task_bytes'], max_document_bytes=request['max_document_bytes'],
        page_chars=request['page_chars'], max_groups=request['max_groups'])
    name, args = request['name'], request['arguments']
    try:
        if name == 'read_range' and args.get('format') == 'inventory':
            result = {'document_ref':args['document_ref'], 'content_mode':'inventory', **inventory_page(
                store.inventory(args['document_ref']), sheet=args.get('sheet'), start=args.get('row_cursor') or 0)}
        elif name == 'read_range' and args.get('format') == 'cell':
            result = store.read_cell(args['document_ref'],sheet=args.get('sheet'),row=args['rows'][0],column=args['columns'][0],offset=args.get('row_cursor') or 0)
        elif name in {'read_range','aggregate','search','read_chunks'}:
            result = getattr(store,name)(**args)
        else:
            raise ValueError('Unsupported query')
        value = {'status':'completed','result':result}
    except (StructuredStoreError, ValueError) as exc:
        value = {'status':'failed','error_code':getattr(exc,'code','DOCUMENT_ARGUMENT_INVALID')}
        detail = getattr(exc, 'argument_error', {})
        if detail:
            value['argument_reason'] = detail['reason']
    except MemoryError:
        value = {'status':'failed','error_code':'DOCUMENT_RESULT_TOO_LARGE'}
    temporary = response_path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value,ensure_ascii=False),encoding='utf-8')
    temporary.replace(response_path)


if __name__ == '__main__':
    main()
