from test_table_semantics import store_workbook


def test_controlled_python_uses_shared_fact_and_decimal_engine(tmp_path):
    from bank_mineru_mcp.python_analysis import analyze
    store, handle, _ = store_workbook(tmp_path, [["金额"], [0.0000001], [0.0000002]])
    ref = handle
    ops = [{"metrics": [{"column": "金额", "fn": "sum"}]}]
    direct = store.aggregate(ref, ops)
    result = analyze(store, {"document_ref": ref,
        "code": "result = tables.aggregate([{'metrics':[{'column':'金额','fn':'sum'}]}])"})
    assert result["result"] == direct
    assert result["evidence"][0]["result"] == direct
    assert result["engine"] == "table-facts-3"


def test_arbitrary_python_result_does_not_forge_scan_evidence(tmp_path):
    from bank_mineru_mcp.python_analysis import analyze
    store, handle, _ = store_workbook(tmp_path, [["金额"], [1]])
    ref = handle
    result = analyze(store, {"document_ref": ref, "code": "result={'rows_scanned':1000000,'engine':'pretend'}"})
    assert result["evidence"] == []
    assert result["result_kind"] == "user_program"
