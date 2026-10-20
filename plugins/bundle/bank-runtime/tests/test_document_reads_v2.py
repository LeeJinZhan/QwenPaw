"""Reading v2 ledger: observed coverage and scoped aggregate evidence."""

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


def test_reparse_migrates_schema_failure_budget_and_current_success_recovers():
    ledger=DocumentReadLedger();observe_parse(ledger)
    name='MinerU__aggregate'
    ledger.reject_call_arguments(name,{'document_ref':REF})
    ledger.reject_call_arguments(name,{'document_ref':REF})
    item=parse_item();item['document_ref']='new_ref'
    ledger.observe('MinerU__parse_documents',{'documents':[{'file_id':'f1'}]},blocks({'items':[item]}),True)
    assert (name,REF) not in ledger.call_argument_failures
    assert ledger.call_argument_failures[(name,'new_ref')]==2
    doc=ledger.documents['new_ref'];doc.aggregates.append({'sources':[]})
    ledger.recover_reference_argument(name,{'document_ref':'new_ref','ops':[{}]},[])
    assert not ledger.call_argument_failures


def test_repeated_verified_source_range_stops_loop_without_becoming_missing_data():
    ledger = DocumentReadLedger(); observe_parse(ledger)
    args = {'document_ref': REF, 'sheet': '支行01', 'rows': [2,2], 'format': 'source'}
    value = {'document_ref': REF, 'sheet': '支行01', 'coordinate_space': 'source',
        'records': [{'row': 2, 'source_row': 2, 'values': ['00880000000000000001']}],
        'rows_returned': [2,2], 'columns': ['账号'], 'has_more': False, 'all_columns': True, 'signature': 'sig'}
    for _ in range(4):
        ledger.start('MinerU__read_range', args)
        ledger.observe('MinerU__read_range', args, blocks(value), True)
    assert not ledger.pending
    assert ledger.successful_read_repeats
    assert not ledger.documents[REF].complete and not ledger.sources_complete(['f1'])


def test_source_coverage_keeps_tail_gap_and_merges_verified_pages():
    ledger=DocumentReadLedger()
    item=parse_item()
    item['inventory']['sheets'][0]['source_rows']=21
    ledger.observe('MinerU__parse_documents',{'documents':[{'file_id':'f1'}]},blocks({'items':[item]}),True)
    def page(start,end,total=21):
        args={'document_ref':REF,'sheet':'支行01','rows':[start,end],'format':'source'}
        value={'document_ref':REF,'sheet':'支行01','coordinate_space':'source','rows_returned':[start,end],
            'sheet_total_rows':total,'all_columns':True,'signature':'sig','has_more':False,
            'records':[{'source_row':i,'row':i,'values':[i]} for i in range(start,end+1)]}
        ledger.observe('MinerU__read_range',args,blocks(value),True)
    page(1,20)
    sheet=ledger.evidence_snapshot()['documents'][0]['sheets'][0]
    assert sheet['source_total_rows']==21 and sheet['source_complete'] is False
    assert sheet['source_covered_ranges']==[[1,20]]
    page(21,21,total=999)  # contradictory totals must never close the gap
    assert ledger.evidence_snapshot()['documents'][0]['sheets'][0]['source_complete'] is False
    page(21,21)
    assert ledger.evidence_snapshot()['documents'][0]['sheets'][0]['source_complete'] is True
    assert ledger.documents[REF].sheet_full('支行01') is False


def test_search_policy_denial_cannot_be_erased_by_successful_read_of_same_document():
    ledger = DocumentReadLedger(); observe_parse(ledger)
    args = {'document_ref': REF, 'query': 'target'}
    ledger.observe('MinerU__search', args, blocks({'status':'failed','error_code':'FILE_ACCESS_DENIED'}), False)
    assert ledger.pending and ledger.error_code == 'ARTIFACT_OUTPUT_MISSING'
    ledger.observe('MinerU__read_range', {'document_ref': REF, 'sheet':'支行01'}, blocks(range_result('支行01',1,20)), True)
    assert ledger.pending and not ledger.permits_scoped_answer('仅已核验范围。')


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


def test_read_range_coverage_records_partial_scope():
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
    snapshot = ledger.evidence_snapshot()['documents'][0]
    assert snapshot['sheets'][0]['covered_ranges'] == [[1, 10]]
    assert snapshot['sheets'][0]['complete'] is False
    assert not ledger.sources_complete(['f1'])



def test_inventory_without_reads_does_not_prove_full_source():
    ledger = DocumentReadLedger()
    observe_parse(ledger)
    assert not ledger.sources_complete(['f1'])
    snapshot = ledger.evidence_snapshot()['documents'][0]
    assert snapshot['statistics'] == []
    assert all(sheet['covered_ranges'] == [] for sheet in snapshot['sheets'])


def test_repeated_completed_range_is_deduplicated_without_becoming_unread():
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
    assert ledger.successful_read_repeats
    assert ledger.documents[REF].no_progress == 3
    assert ledger.documents[REF].covered_rows("支行01") == 5


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
    assert ledger.evidence_snapshot()['documents'][0]['sheets'][0]['complete'] is True
    assert not ledger.sources_complete(['f1'])  # second sheet is still unread


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
    assert ledger.sources_complete(['f1'])
    assert all(sheet['complete'] for sheet in ledger.evidence_snapshot()['documents'][0]['sheets'])


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
    statistic = ledger.evidence_snapshot()['documents'][0]['statistics'][0]
    assert statistic['filter'] == {'column': '姓名', 'op': 'eq', 'value': '甲'}
    assert statistic['rows_matched'] == 1
    assert statistic['sources'] == [{'sheet': '支行01', 'range': [1, 20], 'rows_scanned': 20}]
    assert not ledger.sources_complete(['f1'])


def test_full_aggregate_records_only_its_statistic_not_whole_content():
    ledger=DocumentReadLedger(); observe_parse(ledger)
    aggregate_evidence(ledger)
    assert not ledger.documents[REF].sheet_full('支行01')
    statistic = ledger.evidence_snapshot()['documents'][0]['statistics'][0]
    assert statistic['metrics'] == [{'column': '金额', 'fn': 'sum'}]
    assert statistic['filter'] is None
    assert statistic['sources'] == [{'sheet': '支行01', 'range': [1, 20], 'rows_scanned': 20}]
    assert not ledger.sources_complete(['f1'])


def test_union_total_does_not_become_first_sheet_total():
    ledger=DocumentReadLedger(); observe_parse(ledger)
    aggregate_evidence(ledger,union=True)
    assert ledger.documents[REF].touched == {'支行01','支行02'}
    statistics = ledger.evidence_snapshot()['documents'][0]['statistics']
    assert len(statistics) == 1
    assert statistics[0]['sources'] == [
        {'sheet': '支行01', 'range': [1, 20], 'rows_scanned': 20},
        {'sheet': '支行02', 'range': [1, 20], 'rows_scanned': 20},
    ]
    assert statistics[0]['metrics'] == [{'column': '金额', 'fn': 'sum'}]
    assert not ledger.sources_complete(['f1'])


def test_aggregate_evidence_does_not_expand_to_other_columns_or_functions():
    ledger = DocumentReadLedger(); observe_parse(ledger)
    ledger.documents[REF].column_names = {"金额", "营销笔数", "姓名"}
    aggregate_evidence(ledger)
    statistics = ledger.evidence_snapshot()['documents'][0]['statistics']
    assert [metric for item in statistics for metric in item['metrics']] == [{'column': '金额', 'fn': 'sum'}]
    assert ledger.documents[REF].aggregates[0]['groups'] == [{'金额:sum': 3}]


def test_filtered_statistic_retains_explicit_filter_without_raw_read():
    ledger = DocumentReadLedger(); observe_parse(ledger)
    aggregate_evidence(ledger, filtered=True)
    statistic = ledger.evidence_snapshot()['documents'][0]['statistics'][0]
    assert statistic['filter'] == {'column': '姓名', 'op': 'eq', 'value': '甲'}
    assert statistic['metrics'] == [{'column': '金额', 'fn': 'sum'}]
    assert statistic['rows_matched'] == 1
    assert not ledger.documents[REF].sheet_full("支行01")


def test_paged_groups_are_progress_and_only_complete_after_all_pages():
    ledger = DocumentReadLedger()
    observe_parse(ledger)
    metrics = [{'column':'金额','fn':'sum'}]
    def page(offset):
        op = {'sheet':'支行01','group_by':['类别'],'metrics':metrics,'group_cursor':offset}
        result = {'sources':[{'sheet':'支行01','range':[1,20],'rows_scanned':20}],
            'metrics':metrics,'group_by':['类别'],'filter':None,'group_count':3,
            'groups':[{'group':{'类别':str(offset)},'金额:sum':1}],
            'groups_complete':False,'next_group_cursor':offset+1 if offset<2 else None}
        payload={'document_ref':REF,'ops':[op]}
        ledger.start('MinerU__aggregate',payload)
        ledger.observe('MinerU__aggregate',payload,blocks({'document_ref':REF,'results':[result],'truncated':True}),True)
    page(0)
    assert ledger.pending
    assert ledger.error_code == 'DOCUMENT_READ_INCOMPLETE'
    assert not ledger.documents[REF].aggregates
    page(2)
    assert not ledger.documents[REF].aggregates
    page(0)  # a replay must not fill the missing middle page
    assert not ledger.documents[REF].aggregates
    page(1)
    assert not ledger.pending
    assert len(ledger.documents[REF].aggregates)==1
    assert not ledger.documents[REF].sheet_full('支行01')
    snapshot = ledger.evidence_snapshot()['documents'][0]
    assert snapshot['pending_statistics'] is False
    assert snapshot['statistics'][0]['group_by'] == ['类别']
    assert snapshot['statistics'][0]['metrics'] == metrics


def test_corrected_aggregate_clears_argument_failure_without_claiming_raw_coverage():
    ledger = DocumentReadLedger()
    observe_parse(ledger)
    payload = {'document_ref': REF, 'ops':[{'metrics':[{'column':'金额','op':'sum'}]}]}
    ledger.start('MinerU__aggregate', payload)
    ledger.observe('MinerU__aggregate', payload, blocks({'status':'failed','error_code':'DOCUMENT_ARGUMENT_INVALID'}), False)
    assert ledger.pending and not ledger.argument_retry_exhausted
    aggregate_evidence(ledger)
    assert not ledger.pending and not ledger.argument_retry_exhausted
    assert not ledger.documents[REF].sheet_full('支行01')


def test_argument_budget_and_recovery_are_independent_between_sheets():
    ledger = DocumentReadLedger(); observe_parse(ledger)
    for sheet in ('支行01', '支行02'):
        payload = {'document_ref':REF,'ops':[{'sheet':sheet,'metrics':[{'column':'错误列','fn':'sum'}]}]}
        ledger.start('MinerU__aggregate', payload)
        ledger.observe('MinerU__aggregate', payload, blocks({'status':'failed','error_code':'DOCUMENT_ARGUMENT_INVALID'}), False)
    assert not ledger.argument_retry_exhausted, 'Two sheets are not two retries of one operation'
    aggregate_evidence(ledger)
    assert ledger.pending, 'Success in sheet 1 cannot erase the sheet 2 failure'
    assert ledger.error_code == 'DOCUMENT_ARGUMENT_INVALID'


def test_corrected_argument_cannot_clear_different_filter_failure():
    ledger = DocumentReadLedger(); observe_parse(ledger)
    payload = {'document_ref':REF,'ops':[{'sheet':'支行01','filter':{'column':'姓名','op':'eq','value':'甲'},
        'metrics':[{'column':'错误列','fn':'sum'}]}]}
    ledger.start('MinerU__aggregate', payload)
    ledger.observe('MinerU__aggregate', payload, blocks({'status':'failed','error_code':'DOCUMENT_ARGUMENT_INVALID'}), False)
    aggregate_evidence(ledger)
    assert ledger.pending


def test_different_metric_column_does_not_clear_failed_statistic():
    ledger = DocumentReadLedger(); observe_parse(ledger)
    payload = {'document_ref':REF,'ops':[{'sheet':'支行01','metrics':[{'column':'其它金额','fn':'sum'}]}]}
    ledger.start('MinerU__aggregate', payload)
    ledger.observe('MinerU__aggregate', payload, blocks({'status':'failed','error_code':'DOCUMENT_ARGUMENT_INVALID'}), False)
    aggregate_evidence(ledger)
    assert ledger.pending and ledger.error_code == 'DOCUMENT_ARGUMENT_INVALID'


def test_corrected_missing_metrics_can_recover_same_sheet():
    ledger=DocumentReadLedger(); observe_parse(ledger)
    payload={'document_ref':REF,'ops':[{'sheet':'支行01'}]}
    ledger.start('MinerU__aggregate',payload)
    ledger.observe('MinerU__aggregate',payload,blocks({'status':'failed','error_code':'DOCUMENT_ARGUMENT_INVALID'}),False)
    aggregate_evidence(ledger)
    assert not ledger.pending


def test_corrected_unknown_field_can_recover_same_statistic():
    ledger=DocumentReadLedger(); observe_parse(ledger)
    payload={'document_ref':REF,'ops':[{'sheet':'支行01','limit':10,'metrics':[{'column':'金额','fn':'sum'}]}]}
    ledger.start('MinerU__aggregate',payload)
    ledger.observe('MinerU__aggregate',payload,blocks({'status':'failed','error_code':'DOCUMENT_ARGUMENT_INVALID'}),False)
    aggregate_evidence(ledger)
    assert not ledger.pending


def test_corrected_function_case_can_recover_same_statistic():
    ledger=DocumentReadLedger(); observe_parse(ledger)
    payload={'document_ref':REF,'ops':[{'sheet':'支行01','metrics':[{'column':'金额','fn':'SUM'}]}]}
    ledger.start('MinerU__aggregate',payload)
    ledger.observe('MinerU__aggregate',payload,blocks({'status':'failed','error_code':'DOCUMENT_ARGUMENT_INVALID'}),False)
    aggregate_evidence(ledger)
    assert not ledger.pending


def test_corrected_unknown_function_recovers_without_clearing_another_column():
    ledger=DocumentReadLedger(); observe_parse(ledger)
    payload={'document_ref':REF,'ops':[{'sheet':'支行01','metrics':[{'column':'金额','fn':'total'}]}]}
    ledger.start('MinerU__aggregate',payload)
    ledger.observe('MinerU__aggregate',payload,blocks({'status':'failed','error_code':'DOCUMENT_ARGUMENT_INVALID'}),False)
    aggregate_evidence(ledger)
    assert not ledger.pending


def test_corrected_partial_metric_shape_recovers_only_matching_known_metrics():
    for malformed in ([{'column':'金额','fn':'sum'}, {'column':'姓名','fn':'total'}], ['sum'],
                      [{'column':'金额','fn':'count','op':'sum'}]):
        ledger=DocumentReadLedger(); observe_parse(ledger)
        payload={'document_ref':REF,'ops':[{'sheet':'支行01','metrics':malformed}]}
        ledger.start('MinerU__aggregate',payload)
        ledger.observe('MinerU__aggregate',payload,blocks({'status':'failed','error_code':'DOCUMENT_ARGUMENT_INVALID'}),False)
        metrics=([{'column':'金额','fn':'sum'}, {'column':'姓名','fn':'avg'}]
                 if len(malformed)==2 else [{'column':'金额','fn':'sum'}])
        corrected={'document_ref':REF,'ops':[{'sheet':'支行01','metrics':metrics}]}
        result={'sources':[{'sheet':'支行01','range':[1,20],'rows_scanned':20}],
                'metrics':metrics,'group_by':[],'filter':None,'groups':[{'金额:sum':3}]}
        ledger.start('MinerU__aggregate',corrected)
        ledger.observe('MinerU__aggregate',corrected,blocks({'document_ref':REF,'results':[result],'truncated':False}),True)
        assert not ledger.pending, malformed


def test_partial_metric_repair_cannot_change_a_known_sibling_computation():
    ledger=DocumentReadLedger(); observe_parse(ledger)
    payload={'document_ref':REF,'ops':[{'sheet':'支行01','metrics':[
        {'column':'金额','fn':'avg'}, {'column':'姓名','fn':'total'}]}]}
    ledger.start('MinerU__aggregate',payload)
    ledger.observe('MinerU__aggregate',payload,blocks({'status':'failed','error_code':'DOCUMENT_ARGUMENT_INVALID'}),False)
    aggregate_evidence(ledger)
    assert ledger.pending


def test_evidence_snapshot_reports_scope_without_free_text_judgment():
    ledger = DocumentReadLedger()
    observe_parse(ledger)
    snapshot = ledger.evidence_snapshot()
    assert snapshot['gaps'] == [
        {'kind': 'sheet', 'file_id': 'f1', 'target': '支行01', 'impact': 'scope_unread'},
        {'kind': 'sheet', 'file_id': 'f1', 'target': '支行02', 'impact': 'scope_unread'},
    ]
    assert snapshot['documents'][0]['complete'] is False
    assert snapshot['documents'][0]['statistics'] == []
    assert not ledger.sources_complete(['f1', 'unseen-source'])


def test_reparse_same_version_preserves_confirmed_ranges():
    ledger = DocumentReadLedger()
    observe_parse(ledger)
    ledger.observe('MinerU__read_range', {'document_ref': REF, 'sheet':'支行01'}, blocks(range_result('支行01',1,10)), True)
    observe_parse(ledger)
    assert ledger.documents[REF].covered_rows('支行01') == 10


def test_real_range_progress_resets_stall_budget_but_repeated_pages_do_not():
    ledger = DocumentReadLedger()
    observe_parse(ledger)
    for _ in range(4):
        ledger.start('MinerU__read_range', {'document_ref':REF})
        ledger.observe('MinerU__read_range', {'document_ref':REF}, blocks(range_result('支行01',1,5)), True)
    assert ledger.pending
    assert ledger.error_code == 'DOCUMENT_READ_NO_PROGRESS'
    ledger.start('MinerU__read_range', {'document_ref':REF})
    ledger.observe('MinerU__read_range', {'document_ref':REF}, blocks(range_result('支行01',6,10)), True)
    assert ledger.documents[REF].no_progress == 0
    assert not ledger.pending


def test_empty_sheets_do_not_invalidate_full_workbook_statistics():
    ledger = DocumentReadLedger(); observe_parse(ledger)
    doc = ledger.documents[REF]
    doc.inventory['支行02'] = 0
    aggregate_evidence(ledger)
    snapshot = ledger.evidence_snapshot()['documents'][0]
    assert snapshot['sheets'][1]['complete'] is True
    assert snapshot['statistics'][0]['sources'] == [{'sheet': '支行01', 'range': [1, 20], 'rows_scanned': 20}]
    assert not ledger.sources_complete(['f1'])


def test_category_counts_preserve_exact_group_and_metric_scope_without_raw_read():
    ledger = DocumentReadLedger(); observe_parse(ledger)
    doc = ledger.documents[REF]
    doc.inventory = {'Sheet1': 7445, 'Sheet2': 0, 'Sheet3': 0}
    doc.column_names = {'问题大类', '问题小类', '问题描述', '风险等级'}
    metrics = [{'column': '问题描述', 'fn': 'count'}]
    groups = ['问题大类', '问题小类']
    payload = {'document_ref': REF, 'ops': [{'sheet': 'Sheet1', 'group_by': groups, 'metrics': metrics}]}
    result = {'sources': [{'sheet': 'Sheet1', 'range': [1, 7445], 'rows_scanned': 7445}],
              'metrics': metrics, 'group_by': groups, 'filter': None,
              'groups': [{'group': {'问题大类': '信贷', '问题小类': '贷后'}, '问题描述:count': 7445}]}
    ledger.observe('MinerU__aggregate', payload, blocks({'document_ref': REF, 'results': [result], 'truncated': False}), True)
    statistics = ledger.evidence_snapshot()['documents'][0]['statistics']
    assert len(statistics) == 1
    assert statistics[0]['metrics'] == metrics
    assert statistics[0]['group_by'] == groups
    assert statistics[0]['filter'] is None
    assert statistics[0]['sources'] == [{'sheet': 'Sheet1', 'range': [1, 7445], 'rows_scanned': 7445}]
    assert doc.aggregates[0]['groups'] == result['groups']
    assert not doc.complete


def test_grouped_statistics_keep_exact_units_columns_and_functions_without_raw_read():
    ledger = DocumentReadLedger(); observe_parse(ledger)
    doc = ledger.documents[REF]
    doc.inventory = {'台账': 12}
    doc.column_names = {'记录编号', '风险等级', '金额（元）', '余额（元）'}
    metrics = [{'column': '记录编号', 'fn': 'count'}, {'column': '金额（元）', 'fn': 'sum'},
               {'column': '金额（元）', 'fn': 'avg'}]
    payload = {'document_ref': REF, 'ops': [{'sheet': '台账', 'group_by': ['风险等级'], 'metrics': metrics}]}
    result = {'sources': [{'sheet': '台账', 'range': [1, 12], 'rows_scanned': 12}],
              'metrics': metrics, 'group_by': ['风险等级'], 'filter': None,
              'groups': [{'group': {'风险等级': '高'}, '记录编号:count': 4, '金额（元）:sum': 2200, '金额（元）:avg': 550}]}
    ledger.observe('MinerU__aggregate', payload, blocks({'document_ref': REF, 'results': [result], 'truncated': False}), True)
    statistic = ledger.evidence_snapshot()['documents'][0]['statistics'][0]
    assert statistic['metrics'] == metrics
    assert statistic['group_by'] == ['风险等级']
    assert statistic['sources'] == [{'sheet': '台账', 'range': [1, 12], 'rows_scanned': 12}]
    assert statistic['filter'] is None
    assert doc.aggregates[0]['groups'] == result['groups']
    assert not doc.complete and not ledger.pending
    # An additional mean does not create evidence for a sum on that column.
    additional_metrics = [{'column': '余额（元）', 'fn': 'avg'}]
    additional_payload = {'document_ref': REF, 'ops': [{
        'sheet': '台账', 'group_by': ['风险等级'], 'metrics': additional_metrics}]}
    additional_result = {**result, 'metrics': additional_metrics,
                         'groups': [{'group': {'风险等级': '高'}, '余额（元）:avg': 75}]}
    ledger.observe('MinerU__aggregate', additional_payload,
                   blocks({'document_ref': REF, 'results': [additional_result], 'truncated': False}), True)
    statistics = ledger.evidence_snapshot()['documents'][0]['statistics']
    assert [metric for item in statistics for metric in item['metrics']] == metrics + additional_metrics
    assert not ledger.sources_complete(['f1'])
    # A supported table cannot clear an independent failed raw read.
    ledger.start('MinerU__read_range', {'document_ref': REF, 'sheet': '台账'})
    ledger.observe('MinerU__read_range', {'document_ref': REF, 'sheet': '台账'},
                   blocks({'status': 'failed', 'error_code': 'DOCUMENT_REF_EXPIRED'}), False)
    assert ledger.pending
    assert ledger.failures


def test_distinct_column_projections_are_progress_without_raw_full_coverage():
    ledger = DocumentReadLedger(); observe_parse(ledger)
    for column in ['姓名', '类别', '金额', '日期']:
        payload = {'document_ref': REF, 'sheet': '支行01', 'columns': [column]}
        result = {**range_result('支行01', 1, 5), 'all_columns': False}
        ledger.start('MinerU__read_range', payload)
        ledger.observe('MinerU__read_range', payload, blocks(result), True)
    assert not ledger.pending
    assert ledger.documents[REF].covered_rows('支行01') == 0


def test_repeating_verified_statistics_does_not_turn_them_into_unread_content():
    ledger = DocumentReadLedger(); observe_parse(ledger)
    for _ in range(5):
        aggregate_evidence(ledger)
    assert ledger.documents[REF].no_progress == 0
    assert ledger.documents[REF].repeated_statistics
    assert not ledger.pending
    statistics = ledger.evidence_snapshot()['documents'][0]['statistics']
    assert len(statistics) == 1
    assert statistics[0]['sources'] == [{'sheet': '支行01', 'range': [1, 20], 'rows_scanned': 20}]
    assert statistics[0]['metrics'] == [{'column': '金额', 'fn': 'sum'}]
    assert not ledger.sources_complete(['f1'])
    payload = {'document_ref':REF, 'sheet':'支行02'}
    ledger.start('MinerU__read_range', payload)
    ledger.observe('MinerU__read_range', payload, blocks({'status':'failed', 'error_code':'DOCUMENT_REF_EXPIRED'}), False)
    assert ledger.pending


def test_row_count_evidence_retains_count_value_and_single_source_scope():
    ledger = DocumentReadLedger(); observe_parse(ledger)
    metrics = [{'column':'姓名', 'fn':'count'}]
    payload = {'document_ref':REF, 'ops':[{'sheet':'支行01', 'metrics':metrics}]}
    result = {'sources':[{'sheet':'支行01', 'range':[1,20], 'rows_scanned':20}],
              'metrics':metrics, 'group_by':[], 'filter':None, 'rows_matched':20,
              'groups':[{'姓名:count':20}]}
    ledger.observe('MinerU__aggregate', payload, blocks({'document_ref':REF, 'results':[result], 'truncated':False}), True)
    statistics = ledger.evidence_snapshot()['documents'][0]['statistics']
    assert len(statistics) == 1
    assert statistics[0]['metrics'] == [{'column': '姓名', 'fn': 'count'}]
    assert statistics[0]['sources'] == [{'sheet': '支行01', 'range': [1, 20], 'rows_scanned': 20}]
    assert statistics[0]['rows_matched'] == 20
    assert statistics[0]['group_by'] == []
    assert ledger.documents[REF].aggregates[0]['groups'] == [{'姓名:count': 20}]
    assert not ledger.sources_complete(['f1'])


def repeat_raw_range(ledger, requested_end=20):
    payload = {'document_ref': REF, 'sheet': '支行01', 'rows': [1, requested_end]}
    for _ in range(4):
        ledger.start('MinerU__read_range', payload)
        ledger.observe('MinerU__read_range', payload, blocks(range_result('支行01', 1, 5)), True)


def grouped_page(ledger, offset=0, *, with_other_statistic=False):
    metrics = [{'column': '金额', 'fn': 'sum'}]
    op = {'sheet': '支行01', 'group_by': ['类别'], 'metrics': metrics, 'group_cursor': offset}
    result = {'sources': [{'sheet': '支行01', 'range': [1, 20], 'rows_scanned': 20}],
              'metrics': metrics, 'group_by': ['类别'], 'filter': None, 'group_count': 3,
              'groups': [{'group': {'类别': str(offset)}, '金额:sum': 1}],
              'groups_complete': False, 'next_group_cursor': offset + 1 if offset < 2 else None}
    ops, results = [op], [result]
    if with_other_statistic:
        other = {'sheet': '支行02', 'metrics': [{'column': '金额', 'fn': 'count'}]}
        ops.append(other)
        results.append({'sources': [{'sheet': '支行02', 'range': [1, 20], 'rows_scanned': 20}],
                        'metrics': other['metrics'], 'group_by': [], 'filter': None,
                        'groups': [{'金额:count': 20}]})
    payload = {'document_ref': REF, 'ops': ops}
    ledger.start('MinerU__aggregate', payload)
    ledger.observe('MinerU__aggregate', payload,
                   blocks({'document_ref': REF, 'results': results, 'truncated': True}), True)


def test_successful_statistics_do_not_disable_or_reset_raw_read_stalls():
    for statistics_first in (True, False):
        ledger = DocumentReadLedger(); observe_parse(ledger)
        if statistics_first:
            aggregate_evidence(ledger)
        repeat_raw_range(ledger)
        if not statistics_first:
            aggregate_evidence(ledger)
        assert ledger.pending
        assert ledger.error_code == 'DOCUMENT_READ_NO_PROGRESS'
        assert ledger.documents[REF].no_progress == 3


def test_other_statistics_and_raw_progress_do_not_clear_stalled_group_pages():
    ledger = DocumentReadLedger(); observe_parse(ledger)
    ledger.documents[REF].column_names = {'金额', '类别'}
    aggregate_evidence(ledger)
    grouped_page(ledger)
    grouped_page(ledger)
    grouped_page(ledger)
    grouped_page(ledger, with_other_statistic=True)
    assert ledger.pending
    assert ledger.error_code == 'DOCUMENT_READ_NO_PROGRESS'
    aggregate_evidence(ledger)
    ledger.observe('MinerU__read_range', {'document_ref': REF},
                   blocks(range_result('支行01', 1, 5)), True)
    assert ledger.pending
    grouped_page(ledger, 1)
    assert ledger.pending
    assert ledger.error_code == 'DOCUMENT_READ_INCOMPLETE'
    snapshot = ledger.evidence_snapshot()['documents'][0]
    assert snapshot['pending_statistics'] is True
    assert not any(item.get('group_by') == ['类别'] for item in snapshot['statistics'])
    grouped_page(ledger, 2)
    assert not ledger.pending
    snapshot = ledger.evidence_snapshot()['documents'][0]
    assert snapshot['pending_statistics'] is False
    grouped = [item for item in snapshot['statistics'] if item.get('group_by') == ['类别']]
    assert len(grouped) == 1
    assert grouped[0]['metrics'] == [{'column': '金额', 'fn': 'sum'}]


def test_aggregate_success_cannot_clear_raw_read_failure():
    ledger = DocumentReadLedger(); observe_parse(ledger)
    payload = {'document_ref': REF, 'sheet': '支行02'}
    ledger.start('MinerU__read_range', payload)
    ledger.observe('MinerU__read_range', payload,
                   blocks({'status': 'failed', 'error_code': 'DOCUMENT_REF_EXPIRED'}), False)
    aggregate_evidence(ledger)
    assert ledger.pending
    assert ledger.error_code == 'DOCUMENT_REF_EXPIRED'


def test_partial_office_ocr_unknown_task_blocks_same_source_resubmission():
    for reason in ('MINERU_TIMEOUT', 'MINERU_SUBMIT_AMBIGUOUS'):
        ledger = DocumentReadLedger()
        name = 'MinerU__parse_documents'
        payload = {'documents': [{'file_id': 'f1'}], 'options': {'image_text': True}}
        item = {'file_id': 'f1', 'status': 'completed', 'content_mode': 'inline',
                'markdown': '已读取正文及第一批图片', 'coverage': {'image_text': 'partial'},
                'ocr_batches': {'stop_reason': reason}}
        ledger.start(name, payload)
        ledger.observe(name, payload, blocks({'items': [item]}), True)
        assert ledger.failures['parse:f1'] == reason
        assert ledger.repeated_request(name, payload) == reason
        # Changing parse options must not resubmit an unresolved source job.
        assert ledger.repeated_request(name, {'documents': [{'file_id': 'f1'}]}) == reason
        assert ledger.repeated_request(name, {'documents': [{'file_id': 'f2'}]}) == ''


def test_office_ocr_output_limit_remains_partial_without_remote_job_block():
    ledger = DocumentReadLedger()
    name = 'MinerU__parse_documents'
    payload = {'documents': [{'file_id': 'f1'}], 'options': {'image_text': True}}
    ledger.start(name, payload)
    ledger.observe(name, payload, blocks({'items': [{
        'file_id': 'f1', 'status': 'completed', 'content_mode': 'inline',
        'coverage': {'image_text': 'partial'}, 'ocr_batches': {'stop_reason': 'output_byte_limit'},
    }]}), True)
    assert ledger.failures['parse:f1'] == 'DOCUMENT_READ_INCOMPLETE'
    assert ledger.repeated_request(name, payload) == ''
