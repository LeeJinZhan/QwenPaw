"""Trusted RPC parent for user Python, called only inside the reading container."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from .structured_store import StructuredStoreError
from .table_semantics import ENGINE_VERSION


def analyze(store, arguments):
    code, ref = arguments.get("code"), arguments.get("document_ref")
    if not isinstance(code, str) or not code.strip() or len(code.encode()) > 32000 or set(arguments) != {"code", "document_ref"}:
        raise StructuredStoreError("DOCUMENT_ARGUMENT_INVALID", "Invalid analysis program")
    store.inventory(ref)
    # No host credentials are inherited even in the explicitly selected test adapter.
    environment = {"PYTHONPATH": str(Path(__file__).resolve().parent.parent), "PYTHONDONTWRITEBYTECODE": "1",
                   "PYTHONUNBUFFERED": "1", "HOME": os.environ.get("HOME", "/workspace/scratch"),
                   "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1"}
    process = subprocess.Popen([sys.executable, "-m", "bank_mineru_mcp.table_sdk"], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=environment)
    evidence = []
    try:
        process.stdin.write(json.dumps({"code": code}, ensure_ascii=False) + "\n")
        process.stdin.flush()
        while True:
            line = process.stdout.readline(32001)
            if not line or len(line.encode()) > 32000:
                raise StructuredStoreError("DOCUMENT_ANALYSIS_FAILED", "Analysis channel failed")
            message = json.loads(line)
            if "error_code" in message:
                raise StructuredStoreError("DOCUMENT_ANALYSIS_FAILED", "Analysis program failed")
            if "result" in message:
                return {"document_ref": ref, "engine": ENGINE_VERSION, "result_kind": "user_program",
                        "program_sha256": hashlib.sha256(code.encode()).hexdigest(), "result": message["result"], "evidence": evidence}
            name, args = message.get("request"), message.get("arguments")
            if not isinstance(args, dict) or "document_ref" in args or len(evidence) >= 32:
                raise StructuredStoreError("DOCUMENT_ARGUMENT_INVALID", "Analysis request limit exceeded")
            if name == "inventory" and not args:
                from .inventory import inventory_summary
                result = {"inventory": inventory_summary(store.inventory(ref))}
            elif name in {"aggregate", "read_range"}:
                from .processing_guest import query
                result = query(store, name, {"document_ref": ref, **args})
            else:
                raise StructuredStoreError("DOCUMENT_ARGUMENT_INVALID", "Unregistered analysis operation")
            evidence.append({"name": name, "arguments": args, "result": result})
            if len(json.dumps(evidence, ensure_ascii=False).encode()) > 24000:
                raise StructuredStoreError("DOCUMENT_RESULT_TOO_LARGE", "Analysis evidence quota exceeded")
            process.stdin.write(json.dumps({"result": result}, ensure_ascii=False) + "\n")
            process.stdin.flush()
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        process.stdin.close()
        process.stdout.close()
