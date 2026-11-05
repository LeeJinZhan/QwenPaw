from __future__ import annotations

from bank_mineru_mcp.normalization import normalize_mineru_result


def test_normalization_extracts_official_markdown_and_bounded_chunks() -> None:
    markdown = "# 标题\n\n" + ("正文内容。" * 1200)
    result = normalize_mineru_result(
        {"version": "2.0", "results": {"file_file_001": {"md_content": markdown}}},
        upload_stems={"file_001": "file_file_001"},
        chunk_chars=4000,
    )

    document = result["file_001"]
    assert document.markdown == markdown
    assert len(document.chunks) > 1
    assert all(len(chunk.text) <= 4000 for chunk in document.chunks)
    assert document.title == "标题"


def test_normalization_marks_missing_per_file_result_as_failed() -> None:
    result = normalize_mineru_result(
        {"results": {}},
        upload_stems={"file_001": "file_file_001"},
    )
    assert result["file_001"].error_code == "MINERU_PARSE_FAILED"


def test_empty_tables_and_pipe_prefixed_prose_are_never_discarded():
    for markdown in ['| 姓名 | 金额 |\n| --- | --- |\n', '|这是原文的一行\n',
                     '开头\n|原文1\n|原文2\n正文\n', '```\n| a | b |\n| --- | --- |\n```\n']:
        doc = normalize_mineru_result({'results':{'x':{'md_content':markdown}}}, upload_stems={'f':'x'})['f']
        assert ''.join(chunk.text for chunk in doc.chunks) == markdown


def test_table_pages_repeat_only_a_valid_header_and_preserve_every_row():
    header = '| 姓名 | 金额 |\n| :--- | ---: |\n'
    rows = [f'| 员工{i} | {i} |\n' for i in range(20)]
    doc = normalize_mineru_result({'results':{'x':{'md_content':header+''.join(rows)}}}, upload_stems={'f':'x'},chunk_chars=70)['f']
    assert len(doc.chunks) > 1
    assert all(c.text.startswith(header) for c in doc.chunks)
    assert ''.join(c.text[len(header):] for c in doc.chunks) == ''.join(rows)


def test_large_table_header_and_rows_fit_each_page_without_losing_cells():
    import json
    from dataclasses import asdict
    header = '| ' + '列' * 7400 + ' | 值 |\n| --- | --- |\n'
    rows = [f'| {"值" * 200} | {i} |\n' for i in range(20)]
    document = normalize_mineru_result({'results': {'f': {'md_content': header + ''.join(rows)}}},
        upload_stems={'id': 'f'}, chunk_chars=3200)['id']
    assert all(len(json.dumps(asdict(c), ensure_ascii=False, indent=2).encode()) < 30000 for c in document.chunks)
    assert all(c.text.startswith(header) for c in document.chunks)
    assert ''.join(c.text[len(header):] for c in document.chunks) == ''.join(rows)


def test_oversized_table_header_is_lossless_continuation_not_dropped():
    import json
    from dataclasses import asdict
    markdown = '| ' + '列' * 11000 + ' | 值 |\n| --- | --- |\n| 数据 | 1 |\n'
    document = normalize_mineru_result({'results': {'f': {'md_content': markdown}}}, upload_stems={'id': 'f'})['id']
    assert ''.join(c.text for c in document.chunks) == markdown
    assert len(document.chunks) > 1
    assert all('连续片段' in c.heading for c in document.chunks)
    assert all(len(json.dumps(asdict(c), ensure_ascii=False, indent=2).encode()) < 30000 for c in document.chunks)
