"""Fixed, credential-free entry point of the isolated reading image."""
from __future__ import annotations
import json
import os
from pathlib import Path
import sys

from .spreadsheet import extract_workbook, SpreadsheetExtractError
from .native_office import extract_native_office, SUPPORTED
from .structured_store import StructuredStore, StructuredStoreError
from .inventory import inventory_page


def query(store, name, arguments):
    if name == "read_range" and arguments.get("format") == "inventory":
        return {"document_ref": arguments["document_ref"], "content_mode": "inventory",
                **inventory_page(store.inventory(arguments["document_ref"]), sheet=arguments.get("sheet"), start=arguments.get("row_cursor") or 0)}
    if name == "read_range" and arguments.get("format") == "cell":
        return store.read_cell(arguments["document_ref"], sheet=arguments.get("sheet"), row=arguments["rows"][0],
                               column=arguments["columns"][0], offset=arguments.get("row_cursor") or 0)
    if name in {"read_range", "aggregate", "search", "read_chunks"}:
        return getattr(store, name)(**arguments)
    raise StructuredStoreError("DOCUMENT_ARGUMENT_INVALID", "Unregistered query")


def main():
    os.umask(0o077)
    request = json.loads(sys.stdin.buffer.read(64 * 1024))
    output, work = Path("/workspace/output"), Path("/workspace/scratch")
    os.environ["BANK_READING_WORK_ROOT"] = str(work)
    os.environ["SQLITE_TMPDIR"] = str(work)
    try:
        if request["kind"] == "parse":
            source = Path("/workspace/input") / request["source"]
            if source.suffix.lower() in SUPPORTED:
                extract_native_office(source, output, max_bytes=request["max_bytes"],
                    max_images=request.get('native_image_max_count', 200))
            else:
                inventory = extract_workbook(source, output, stem="document", max_bytes=request["max_bytes"],
                    allow_partial=True, header_row=request["header_row"])
                (output / "inventory.json").write_text(json.dumps(inventory, ensure_ascii=False), encoding="utf-8")
            value = {"status": "ready"}
        elif request["kind"] == "query":
            store = StructuredStore(root=request["root"], process_start_key=bytes.fromhex(request["key"]),
                                    max_document_bytes=request["max_bytes"], max_task_bytes=request["max_bytes"])
            if request["name"] == "analyze":
                from .python_analysis import analyze
                result = analyze(store, request["arguments"])
            else:
                result = query(store, request["name"], request["arguments"])
            value = {"status": "ready", "result": result}
        else:
            raise ValueError("Invalid operation")
        if len(json.dumps(value, ensure_ascii=False, indent=2).encode()) > 32000:
            raise SpreadsheetExtractError("DOCUMENT_RESULT_TOO_LARGE", "Response budget exceeded")
    except (SpreadsheetExtractError, StructuredStoreError) as exc:
        value = {"status": "failed", "error_code": exc.code}
        detail = getattr(exc, 'argument_error', {})
        if detail:
            value['argument_reason'] = detail['reason']
    except (MemoryError, OSError):
        value = {"status": "failed", "error_code": "DOCUMENT_RESULT_TOO_LARGE"}
    except Exception:
        value = {"status": "failed", "error_code": "DOCUMENT_PARSE_FAILED"}
    print(json.dumps(value, ensure_ascii=False))


if __name__ == "__main__":
    main()
