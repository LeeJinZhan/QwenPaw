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
