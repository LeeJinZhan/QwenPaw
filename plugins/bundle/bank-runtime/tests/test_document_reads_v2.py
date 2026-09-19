"""Reading v2 ledger: structured coverage, aggregate evidence, declaration checks."""

from __future__ import annotations

import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bank_runtime.gateway.document_reads import DocumentReadLedger

REF = "ds1_aa_bb"


def blocks(value):
    return [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}]


def parse_item():
    return {
        "file_id": "f1",
        "status": "completed",
        "content_mode": "structured",
        "document_ref": REF,
        "chunk_count": 4,
        "preview": "支行01(20行×4列); 支行02(20行×4列)",
        "inventory": {
            "sheet_count": 2,
            "total_rows": 40,
            "sheets": [
                {"name": "支行01", "rows": 20},
                {"name": "支行02", "rows": 20},
            ],
        },
    }


def observe_parse(ledger):
    ledger.start("MinerU__parse_documents", {"documents": [{"file_id": "f1"}]})
    ledger.observe(
        "MinerU__parse_documents",
        {"documents": [{"file_id": "f1"}]},
        blocks({"items": [parse_item()]}),
        True,
    )


def range_result(sheet, start, end, total=20):
    return {
        "document_ref": REF,
        "sheet": sheet,
        "rows_returned": [start, end],
        "sheet_total_rows": total,
        "rows_scanned": end - start + 1,
        "has_more": end < total,
        "next_row_cursor": end + 1,
        "signature": "sig",
        "markdown": "| x |",
    }


def test_structured_parse_registers_inventory():
    ledger = DocumentReadLedger()
    observe_parse(ledger)
    doc = ledger.documents[REF]
    assert doc.inventory == {"支行01": 20, "支行02": 20}
    assert ledger.pending is False, "structured docs must not force full-read pending"


def test_read_range_coverage_and_declaration_conflict():
    ledger = DocumentReadLedger()
    observe_parse(ledger)
    ledger.start("MinerU__read_range", {"document_ref": REF})
    ledger.observe(
        "MinerU__read_range",
        {"document_ref": REF, "sheet": "支行01", "rows": [1, 10]},
        blocks(range_result("支行01", 1, 10)),
        True,
    )
    doc = ledger.documents[REF]
    assert doc.covered_rows("支行01") == 10
    assert ledger.declaration_conflict("支行01 前 10 行明细如下") == ""
    assert ledger.declaration_conflict("已给出支行01合计与全量统计") == "DOCUMENT_READ_INCOMPLETE"



def test_full_claim_without_evidence_conflicts():
    ledger = DocumentReadLedger()
    observe_parse(ledger)
    assert ledger.declaration_conflict("全量统计结果如下") == "DOCUMENT_READ_INCOMPLETE"


def test_repeated_identical_range_remains_idempotent():
    ledger = DocumentReadLedger()
    observe_parse(ledger)
    for _ in range(4):
        ledger.start("MinerU__read_range", {"document_ref": REF})
        ledger.observe(
            "MinerU__read_range",
            {"document_ref": REF, "sheet": "支行01", "rows": [1, 5]},
            blocks(range_result("支行01", 1, 5)),
            True,
        )
    assert not ledger.pending
    assert ledger.documents[REF].no_progress == 0


def test_rejected_range_does_not_poison_later_ranges():
    ledger = DocumentReadLedger()
    observe_parse(ledger)
    ledger.start("MinerU__read_range", {"document_ref": REF})
    ledger.observe(
        "MinerU__read_range",
        {"document_ref": REF, "sheet": "支行01", "rows": [1, 5]},
        blocks({**range_result("支行01", 1, 5), "sheet_total_rows": 999}),
        True,
    )
    ledger.start("MinerU__read_range", {"document_ref": REF})
    ledger.observe(
        "MinerU__read_range",
        {"document_ref": REF, "sheet": "支行01", "rows": [1, 20]},
        blocks(range_result("支行01", 1, 20)),
        True,
    )
    doc = ledger.documents[REF]
    assert doc.sheet_full("支行01")
    assert ledger.declaration_conflict("支行01合计") == ""


def test_legacy_chunks_record_structured_rows_and_retry():
    ledger = DocumentReadLedger()
    observe_parse(ledger)
    payload = {"document_ref": REF}
    result = {"document_ref": REF, "chunks": [
        {"index": 0, "heading": "支行01", "text": "data", "rows_returned": [1, 20]},
        {"index": 1, "heading": "支行02", "text": "data", "rows_returned": [1, 20]},
    ], "has_more": False, "next_cursor": None,
       "coverage": {"read": 2, "total": 2}}
    for _ in range(2):
        ledger.start("MinerU__read_document_chunks", payload)
        ledger.observe("MinerU__read_document_chunks", payload, blocks(result), True)
        assert not ledger.pending
        assert ledger.documents[REF].complete
    assert ledger.declaration_conflict("全部工作表全量总结") == ""


def test_legacy_chunks_without_row_evidence_cannot_claim_complete():
    ledger = DocumentReadLedger()
    observe_parse(ledger)
    payload = {"document_ref": REF}
    ledger.start("MinerU__read_document_chunks", payload)
    ledger.observe("MinerU__read_document_chunks", payload, blocks({
        "document_ref": REF, "chunks": [{"index": 0, "heading": "支行01", "text": "x"}],
        "has_more": False, "next_cursor": None}), True)
    assert ledger.pending
    assert not ledger.documents[REF].complete


def test_truncated_aggregate_cannot_clear_failed_read():
    ledger = DocumentReadLedger()
    observe_parse(ledger)
    payload = {"document_ref": REF}
    ledger.start("MinerU__aggregate", payload)
    ledger.observe("MinerU__aggregate", payload, blocks({
        "document_ref": REF, "truncated": True,
        "results": [{"sheet": "支行01", "full_range": True, "rows_scanned": 20}],
    }), True)
    assert ledger.pending
    assert not ledger.documents[REF].sheet_full("支行01")


def test_range_recovery_clears_legacy_failure():
    ledger = DocumentReadLedger()
    observe_parse(ledger)
    ledger.start("MinerU__read_document_chunks", {"document_ref": REF})
    ledger.observe("MinerU__read_document_chunks", {"document_ref": REF}, blocks({"status": "failed"}), False)
    for sheet in ("支行01", "支行02"):
        payload = {"document_ref": REF, "sheet": sheet}
        ledger.start("MinerU__read_range", payload)
        ledger.observe("MinerU__read_range", payload, blocks(range_result(sheet, 1, 20)), True)
    assert not ledger.pending
    assert ledger.documents[REF].complete


def test_selected_columns_do_not_prove_full_sheet_read():
    ledger = DocumentReadLedger()
    observe_parse(ledger)
    payload = {"document_ref": REF, "sheet": "支行01"}
    ledger.start("MinerU__read_range", payload)
    ledger.observe("MinerU__read_range", payload, blocks({**range_result("支行01", 1, 20), "all_columns": False}), True)
    assert not ledger.pending
    assert not ledger.documents[REF].sheet_full("支行01")


def test_argument_failure_is_recoverable_and_does_not_prove_coverage():
    from bank_runtime.gateway.document_reads import result_error, FILE_POLICY_CODES
    ledger = DocumentReadLedger()
    observe_parse(ledger)
    payload = {'document_ref': REF, 'sheet': '支行01', 'columns': ['姓名', '姓名']}
    failed = blocks({'status': 'failed', 'error_code': 'DOCUMENT_ARGUMENT_INVALID'})
    assert result_error(failed) == 'DOCUMENT_ARGUMENT_INVALID'
    assert result_error(failed) not in FILE_POLICY_CODES
    ledger.start('MinerU__read_range', payload)
    ledger.observe('MinerU__read_range', payload, failed, True)
    assert ledger.documents[REF].covered_rows('支行01') == 0
    valid = {'document_ref': REF, 'sheet': '支行01'}
    ledger.start('MinerU__read_range', valid)
    ledger.observe('MinerU__read_range', valid, blocks({**range_result('支行01', 1, 20), 'all_columns': True}), True)
    assert ledger.documents[REF].covered_rows('支行01') == 20
    assert not ledger.pending


def test_formula_failure_cannot_be_reported_as_complete():
    ledger = DocumentReadLedger()
    payload = {'documents': [{'file_id': 'f1'}]}
    ledger.start('MinerU__parse_documents', payload)
    ledger.observe('MinerU__parse_documents', payload, blocks({'status': 'failed', 'items': [
        {'file_id': 'f1', 'status': 'failed', 'error_code': 'DOCUMENT_FORMULA_CACHE_MISSING'}]}), True)
    assert ledger.pending
    assert ledger.error_code == 'DOCUMENT_FORMULA_CACHE_MISSING'
    assert not ledger.documents


def aggregate_evidence(ledger, *, filtered=False, union=False):
    sources = [{'sheet':'支行01','range':[1,20],'rows_scanned':20}]
    if union:
        sources.append({'sheet':'支行02','range':[1,20],'rows_scanned':20})
    metrics = [{'column':'金额','fn':'sum'}]
    rule = {'column':'姓名','op':'eq','value':'甲'} if filtered else None
    op = {'metrics':metrics, **({'filter':rule} if rule else {}),
          **({'cross_sheet_union':{'key_column':'姓名'}} if union else {'sheet':'支行01'})}
    result = {'sheet':'*' if union else '支行01','rows_scanned':40 if union else 20,
              'rows_matched':1 if filtered else (40 if union else 20), 'full_range':True,
              'range':None if union else [1,20], 'sources':sources, 'filter':rule,
              'metrics':metrics,'groups':[{'金额:sum':3}]}
    payload = {'document_ref':REF,'ops':[op]}
    ledger.observe('MinerU__aggregate',payload,blocks({'document_ref':REF,'results':[result],'truncated':False}),True)


def test_filtered_aggregate_is_never_complete_read_or_unfiltered_total_evidence():
    ledger=DocumentReadLedger(); observe_parse(ledger)
    aggregate_evidence(ledger,filtered=True)
    assert not ledger.documents[REF].sheet_full('支行01')
    assert ledger.declaration_conflict('支行01金额合计为3') == 'DOCUMENT_READ_INCOMPLETE'


def test_full_aggregate_authorizes_only_its_statistic_not_whole_content():
    ledger=DocumentReadLedger(); observe_parse(ledger)
    aggregate_evidence(ledger)
    assert not ledger.documents[REF].sheet_full('支行01')
    assert ledger.declaration_conflict('支行01金额合计为3') == ''
    assert ledger.declaration_conflict('支行01营销笔数合计为3') == 'DOCUMENT_READ_INCOMPLETE'
    assert ledger.declaration_conflict('已完整读取支行01的全部内容') == 'DOCUMENT_READ_INCOMPLETE'


def test_union_total_does_not_become_first_sheet_total():
    ledger=DocumentReadLedger(); observe_parse(ledger)
    aggregate_evidence(ledger,union=True)
    assert ledger.documents[REF].touched == {'支行01','支行02'}
    assert ledger.declaration_conflict('所有工作表金额合计为3') == ''
    assert ledger.declaration_conflict('支行01金额合计为3') == 'DOCUMENT_READ_INCOMPLETE'


def test_aggregate_does_not_support_other_columns_or_functions_in_same_answer():
    ledger = DocumentReadLedger(); observe_parse(ledger)
    ledger.documents[REF].column_names = {"金额", "营销笔数", "姓名"}
    aggregate_evidence(ledger)
    assert ledger.declaration_conflict("支行01金额合计为3，营销笔数合计为9") == "DOCUMENT_READ_INCOMPLETE"
    assert ledger.declaration_conflict("支行01金额合计为3，金额最大为3") == "DOCUMENT_READ_INCOMPLETE"


def test_filtered_statistic_requires_explicit_filter_but_does_not_need_raw_read():
    ledger = DocumentReadLedger(); observe_parse(ledger)
    aggregate_evidence(ledger, filtered=True)
    assert ledger.declaration_conflict("支行01姓名为甲的金额合计为3") == ""
    assert not ledger.documents[REF].sheet_full("支行01")
