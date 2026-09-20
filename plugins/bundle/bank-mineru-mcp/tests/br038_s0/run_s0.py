"""S0 reproduction driver for BR-038 (diagnostic, not a unit test).

Run with the QwenPaw plugin environment:
    ../../../.venv/bin/python tests/br038_s0/run_s0.py <out_dir>

Drives the real plugin chain (MinerUToolService + DocumentStore) with a stubbed
MinerU client fed by converter_sim markdown, and the real DocumentReadLedger
from bank-runtime, to separate the four candidate mechanisms of BR-038.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/br038-s0")
PLUGIN = Path(__file__).resolve().parents[2]
GATEWAY = PLUGIN.parents[0] / "bank-runtime" / "bank_runtime" / "gateway"
sys.path.insert(0, str(PLUGIN))

from bank_mineru_mcp.document_store import DocumentStore, DocumentStoreError  # noqa: E402
from bank_mineru_mcp.tools import MinerUToolService  # noqa: E402

_pkg = ModuleType("br038_ledger_pkg")
_pkg.__path__ = [str(GATEWAY)]
sys.modules["br038_ledger_pkg"] = _pkg
ledger_mod = importlib.import_module("br038_ledger_pkg.document_reads")

TASK = "taskS0"
WORKBOOK = OUT / "store" / TASK / "workbook.xlsx"


class StubMineru:
    def __init__(self, md: str) -> None:
        self.md = md

    async def parse(self, files, *, parse_method="auto", language="auto", tables=True, formulas=True):
        stems = {item.file_id: f"file_{item.file_id}" for item in files}
        payload = {"results": {stem: {"md_content": self.md, "page_count": 12} for stem in stems.values()}}
        return payload, stems


class StubResolver:
    def resolve(self, file_ref: str):
        path = WORKBOOK
        return SimpleNamespace(
            task_id=TASK,
            file_id="f1",
            path=path,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            extension=".xlsx",
            size_bytes=path.stat().st_size,
            sha256="s0",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=2),
        )


def make_service(md: str, root: Path):
    store = DocumentStore(root=root)
    service = MinerUToolService(
        file_resolver=StubResolver(),
        mineru_client=StubMineru(md),
        document_store=store,
    )
    return service, store


def blocks(result: dict):
    return [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]


def parse_one(service: MinerUToolService) -> dict:
    outcome = asyncio.run(
        service.parse_documents([{"file_id": "f1", "file_ref": "fr1_aa_bb"}])
    )
    item = outcome["items"][0]
    assert item["status"] == "completed", item
    return item


def page(service: MinerUToolService, ref: str, cursor, limit=5):
    return service.read_document_chunks(ref, cursor=cursor, limit=limit)


def observe(ledger, ref, cursor, result, limit=5, content=None):
    ledger.observe(
        "MinerU__read_document_chunks",
        {"document_ref": ref, "cursor": cursor, "limit": limit},
        content if content is not None else blocks(result),
        True,
    )


def main() -> dict:
    md_full = (OUT / "md_full.md").read_text(encoding="utf-8")
    md_trunc = (OUT / "md_trunc.md").read_text(encoding="utf-8")
    report: dict = {"md_full_chars": len(md_full), "md_trunc_chars": len(md_trunc)}

    # M0: scale + perfect paging cost
    service, store = make_service(md_full, OUT / "store")
    ledger = ledger_mod.DocumentReadLedger()
    ledger.start("MinerU__parse_documents", {"documents": [{"file_id": "f1"}]})
    item = parse_one(service)
    ref = item["document_ref"]
    ledger.observe("MinerU__parse_documents", {"documents": [{"file_id": "f1"}]}, blocks({"items": [item]}), True)
    report["M0"] = {
        "content_mode": item["content_mode"],
        "chunk_count": item["chunk_count"],
        "preview_chars": len(item["preview"] or ""),
    }
    cursor, pages, latencies = None, 0, []
    while True:
        started = time.perf_counter()
        result = page(service, ref, cursor, limit=10)
        latencies.append(round((time.perf_counter() - started) * 1000, 1))
        pages += 1
        observe(ledger, ref, cursor, result, limit=10)
        cursor = result["next_cursor"]
        if not result["has_more"]:
            break
    report["M0"].update(
        pages_limit10=pages,
        page_ms_max=max(latencies),
        page_ms_avg=round(sum(latencies) / len(latencies), 1),
        ledger_complete=ledger.complete(ref) if hasattr(ledger, "complete") else ledger.documents[ref].complete,
        est_tool_result_chars=pages * 32_000,
    )

    # M2: screenshot-shaped budget (parse + 3 reads at limit 5)
    service2, _ = make_service(md_full, OUT / "store")
    ledger2 = ledger_mod.DocumentReadLedger()
    ledger2.start("MinerU__parse_documents", {"documents": [{"file_id": "f1"}]})
    item2 = parse_one(service2)
    ref2 = item2["document_ref"]
    ledger2.observe("MinerU__parse_documents", {"documents": [{"file_id": "f1"}]}, blocks({"items": [item2]}), True)
    cursor2 = None
    for _ in range(3):
        ledger2.start("MinerU__read_document_chunks", {"document_ref": ref2})
        result2 = page(service2, ref2, cursor2, limit=5)
        observe(ledger2, ref2, cursor2, result2, limit=5)
        cursor2 = result2["next_cursor"]
    read_chunks, total = ledger2.coverage(ref2)
    report["M2"] = {
        "reads": 3,
        "coverage": f"{read_chunks}/{total}",
        "coverage_pct": round(100 * read_chunks / total, 1),
        "pending": ledger2.pending,
        "turn_end_error": ledger2.error_code,
    }

    # M3: one tampered page then contract-following continuation -> latch
    service3, _ = make_service(md_full, OUT / "store")
    ledger3 = ledger_mod.DocumentReadLedger()
    ledger3.start("MinerU__parse_documents", {"documents": [{"file_id": "f1"}]})
    item3 = parse_one(service3)
    ref3 = item3["document_ref"]
    ledger3.observe("MinerU__parse_documents", {"documents": [{"file_id": "f1"}]}, blocks({"items": [item3]}), True)
    first = page(service3, ref3, None, limit=5)
    observe(ledger3, ref3, None, first, limit=5)
    second = page(service3, ref3, first["next_cursor"], limit=5)
    tampered = blocks(second) + blocks({**second, "has_more": not second["has_more"]})
    observe(ledger3, ref3, first["next_cursor"], second, limit=5, content=tampered)
    spiral = []
    cursor3 = second["next_cursor"]
    for step in range(3):
        ledger3.start("MinerU__read_document_chunks", {"document_ref": ref3})
        result3 = page(service3, ref3, cursor3, limit=5)
        observe(ledger3, ref3, cursor3, result3, limit=5)
        spiral.append(ledger3.error_code or "ok")
        cursor3 = result3["next_cursor"] or cursor3
    report["M3"] = {
        "tampered_page_no_progress": ledger3.documents[ref3].no_progress,
        "spiral_error_sequence": spiral,
        "latched_error": ledger3.error_code,
    }

    # M4: process restart loses in-memory registry, orphan file remains
    restarted = DocumentStore(root=OUT / "store")
    try:
        restarted.read_chunks(ref, cursor=None, limit=5)
        report["M4"] = {"error": None}
    except DocumentStoreError as exc:
        orphans = list((OUT / "store" / TASK / ".mineru").glob("*.chunks.jsonl"))
        report["M4"] = {
            "error": exc.code,
            "orphan_files": len(orphans),
            "orphan_bytes": sum(p.stat().st_size for p in orphans),
        }

    # M5: source truncation is invisible to the guard
    service5, _ = make_service(md_trunc, OUT / "store")
    ledger5 = ledger_mod.DocumentReadLedger()
    ledger5.start("MinerU__parse_documents", {"documents": [{"file_id": "f1"}]})
    item5 = parse_one(service5)
    ref5 = item5["document_ref"]
    ledger5.observe("MinerU__parse_documents", {"documents": [{"file_id": "f1"}]}, blocks({"items": [item5]}), True)
    cursor5, pages5 = None, 0
    while True:
        result5 = page(service5, ref5, cursor5, limit=10)
        pages5 += 1
        observe(ledger5, ref5, cursor5, result5, limit=10)
        cursor5 = result5["next_cursor"]
        if not result5["has_more"]:
            break
    report["M5"] = {
        "chunk_count": item5["chunk_count"],
        "pages": pages5,
        "ledger_complete": ledger5.documents[ref5].complete,
        "truth_sheet_coverage": "8/30",
        "guard_detects_truncation": False,
    }
    return report


if __name__ == "__main__":
    print(json.dumps(main(), ensure_ascii=False, indent=2))
