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
    assert ledger.declaration_conflict('支行01按类别分组的金额合计') == ''


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


def test_scope_disclaimers_quotes_and_general_advice_are_not_full_claims():
    ledger = DocumentReadLedger()
    observe_parse(ledger)
    for text in ('尚未完整读取，以下仅说明已读范围。', '这不代表所有工作表。',
                 '整体建议是先核对口径。', '原文写着“全部内容”，这里仅引用其措辞。'):
        assert ledger.declaration_conflict(text) == '', text
    for text in ('尚未完整读取，但所有工作表合计为123。',
                 '这不代表所有工作表。所有工作表合计为123。',
                 '原文写着“全部内容”，我已完整读取全文。'):
        assert ledger.declaration_conflict(text) == 'DOCUMENT_READ_INCOMPLETE', text


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
    assert ledger.declaration_conflict('所有工作表金额合计为3') == ''
    assert ledger.declaration_conflict('所有工作表已完整读取全部内容') == 'DOCUMENT_READ_INCOMPLETE'


def test_category_counts_accept_natural_group_wording_without_claiming_raw_read():
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
    assert ledger.declaration_conflict('按问题大类/问题小类统计全量问题描述计数') == ''
    assert ledger.declaration_conflict('全表按问题大类/问题小类汇总问题描述数量合计7445条') == ''
    assert ledger.declaration_conflict('全表问题描述平均为12') == 'DOCUMENT_READ_INCOMPLETE'
    assert ledger.declaration_conflict('全表按问题大类/问题小类汇总问题描述数量合计7445且问题描述总计9999') == 'DOCUMENT_READ_INCOMPLETE'
    assert ledger.declaration_conflict('按风险等级统计全量问题描述计数') == 'DOCUMENT_READ_INCOMPLETE'
    assert not doc.complete


def test_grouped_markdown_table_keeps_its_header_scope_without_raw_read():
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
    table = ('| 风险等级 | 记录数 | 金额合计（元） | 平均金额（元） |\n'
             '| --- | ---: | ---: | ---: |\n'
             '| 高 | 4 | 2,200.00 | 550.00 |')
    assert ledger.declaration_conflict('按风险等级统计如下：\n\n' + table) == ''
    assert not doc.complete and not ledger.pending
    for unsupported in (
        table.replace('金额合计', '余额合计'),
        table.replace('平均金额', '最大金额'),
        table.replace('金额合计（元）', '金额合计（万元）'),
        table.replace('风险等级', '机构'),
        table + '\n\n已完整读取台账全文。',
        table + '\n\n余额合计为999。',
        table.replace('| 高 |', '| 已读完全文 |'),
    ):
        assert ledger.declaration_conflict(unsupported) == 'DOCUMENT_READ_INCOMPLETE'
    # Evidence for a function on one column cannot prove it on another.
    metrics.append({'column': '余额（元）', 'fn': 'avg'})
    ledger.observe('MinerU__aggregate', payload, blocks({'document_ref': REF, 'results': [result], 'truncated': False}), True)
    assert ledger.declaration_conflict(table.replace('平均金额', '平均余额')) == ''
    assert ledger.declaration_conflict(table.replace('金额合计', '余额合计')) == 'DOCUMENT_READ_INCOMPLETE'
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
    assert ledger.declaration_conflict('支行01金额合计为3') == ''
    assert ledger.declaration_conflict('所有工作表金额合计为3') == 'DOCUMENT_READ_INCOMPLETE'
    assert ledger.declaration_conflict('已读完支行01全部内容') == 'DOCUMENT_READ_INCOMPLETE'
    payload = {'document_ref':REF, 'sheet':'支行02'}
    ledger.start('MinerU__read_range', payload)
    ledger.observe('MinerU__read_range', payload, blocks({'status':'failed', 'error_code':'DOCUMENT_REF_EXPIRED'}), False)
    assert ledger.pending


def test_plain_row_count_total_matches_verified_count_not_numeric_sum():
    ledger = DocumentReadLedger(); observe_parse(ledger)
    metrics = [{'column':'姓名', 'fn':'count'}]
    payload = {'document_ref':REF, 'ops':[{'sheet':'支行01', 'metrics':metrics}]}
    result = {'sources':[{'sheet':'支行01', 'range':[1,20], 'rows_scanned':20}],
              'metrics':metrics, 'group_by':[], 'filter':None, 'rows_matched':20,
              'groups':[{'姓名:count':20}]}
    ledger.observe('MinerU__aggregate', payload, blocks({'document_ref':REF, 'results':[result], 'truncated':False}), True)
    assert ledger.declaration_conflict('支行01合计20条') == ''
    assert ledger.declaration_conflict('支行01合计21条') == 'DOCUMENT_READ_INCOMPLETE'
    assert ledger.declaration_conflict('支行01姓名数量合计21条') == 'DOCUMENT_READ_INCOMPLETE'
    assert ledger.declaration_conflict('支行01金额合计20元') == 'DOCUMENT_READ_INCOMPLETE'
    assert ledger.declaration_conflict('所有工作表合计20条') == 'DOCUMENT_READ_INCOMPLETE'
    assert ledger.declaration_conflict('支行01合计20条且金额总计999') == 'DOCUMENT_READ_INCOMPLETE'


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
    assert ledger.declaration_conflict('支行01按类别分组的金额合计') == 'DOCUMENT_READ_INCOMPLETE'
    grouped_page(ledger, 2)
    assert not ledger.pending
    assert ledger.declaration_conflict('支行01按类别分组的金额合计') == ''


def test_aggregate_success_cannot_clear_raw_read_failure():
    ledger = DocumentReadLedger(); observe_parse(ledger)
    payload = {'document_ref': REF, 'sheet': '支行02'}
    ledger.start('MinerU__read_range', payload)
    ledger.observe('MinerU__read_range', payload,
                   blocks({'status': 'failed', 'error_code': 'DOCUMENT_REF_EXPIRED'}), False)
    aggregate_evidence(ledger)
    assert ledger.pending
    assert ledger.error_code == 'DOCUMENT_REF_EXPIRED'
