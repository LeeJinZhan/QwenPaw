"""Private extraction subprocess entry point. Never a model-callable shell tool."""
import json
import os
from pathlib import Path
import resource
import sys
from .spreadsheet import extract_workbook, SpreadsheetExtractError


def main():
    source, target, quota, memory, state = sys.argv[1:]
    target = Path(target)
    target.mkdir(mode=0o700, parents=True, exist_ok=True)
    # macOS RLIMIT_AS is not reliable. Capacity release tests run in Linux images.
    if sys.platform == 'linux':
        resource.setrlimit(resource.RLIMIT_AS, (int(memory), int(memory)))
    os.umask(0o077)
    result = {}
    try:
        result = {'status': 'ready', 'inventory': extract_workbook(Path(source), target, stem='document', max_bytes=int(quota), allow_partial=True)}
    except SpreadsheetExtractError as exc:
        result = {'status': 'failed', 'error_code': exc.code}
    except MemoryError:
        result = {'status': 'failed', 'error_code': 'DOCUMENT_RESULT_TOO_LARGE'}
    except Exception:
        result = {'status': 'failed', 'error_code': 'DOCUMENT_PARSE_FAILED'}
    temporary = Path(state + '.tmp')
    temporary.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
    temporary.replace(state)


if __name__ == '__main__':
    main()
