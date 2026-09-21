"""Structured spreadsheet extraction with merged-range expansion.

Structured formats (xlsx, csv, tsv) never go through MinerU layout parsing.
This module streams sheets into bounded row-block files plus an inventory so
read_range/aggregate/search can address sheets and row ranges directly.
"""

from __future__ import annotations

import codecs
import csv
import json
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Iterable

BLOCK_ROWS = 200
CHUNK_SIM_ROWS = 25


class SpreadsheetExtractError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class SourceRow(list):
    def __init__(self, values, invalid_columns=()):
        super().__init__(values)
        self.invalid_columns = set(invalid_columns)


def extract_workbook(path: Path, target_dir: Path, *, stem: str,
                     max_bytes: int = 2 * 1024**3, allow_partial: bool = False) -> dict[str, Any]:
    """Spool one sheet at a time; neither rows nor column values accumulate."""
    target_dir.mkdir(parents=True, exist_ok=True)
    suffix = path.suffix.lower()
    csv.field_size_limit(max_bytes)
    if suffix in {".csv", ".tsv"}:
        sheets = _stream_delimited(path, suffix)
    elif suffix == ".xlsx":
        sheets = _stream_xlsx(path, target_dir, allow_partial)
    else:
        raise SpreadsheetExtractError("FILE_TYPE_UNSUPPORTED", "Structured extraction supports xlsx/csv/tsv")
    inventory_sheets = []
    used = 0
    try:
        for index, (name, rows, merged, quality) in enumerate(sheets):
            meta, size = _spool_sheet(target_dir, index, name, rows, merged, quality, max_bytes - used)
            inventory_sheets.append(meta)
            used += size
    finally:
        sheets.close()
    return {"engine": "ooxml-1" if suffix == ".xlsx" else "delimited-1",
            "format_version": 2, "title": path.name, "sheet_count": len(inventory_sheets),
            "total_rows": sum(s["rows"] for s in inventory_sheets), "sheets": inventory_sheets}


def _spool_sheet(target_dir, index, name, rows, merged, quality, budget):
    spool = target_dir / f"sheet_{index:02d}.spool"
    width = 0
    header_row = 0
    header = []
    first = []
    written = 0
    number = 0
    # Merge intervals are processed once on entry/exit, never scanned per cell.
    pending = iter(sorted(merged))
    upcoming = next(pending, None)
    active = []
    with spool.open("wb") as handle:
        for number, raw in enumerate(rows, 1):
            if number == 1:
                first = list(raw)
            values = list(raw)
            invalid = set(getattr(raw, "invalid_columns", ()))
            while upcoming is not None and upcoming[0] <= number:
                r1, c1, r2, c2 = upcoming
                anchor = raw[c1 - 1] if c1 <= len(raw) else None
                active.append((r1, c1, r2, c2, anchor, c1 - 1 in invalid))
                upcoming = next(pending, None)
            active = [m for m in active if m[2] >= number]
            for r1, c1, r2, c2, anchor, anchor_invalid in active:
                if len(values) < c2:
                    values.extend([None] * (c2 - len(values)))
                for col in range(c1 - 1, c2):
                    values[col] = anchor
                    if anchor_invalid:
                        invalid.add(col)
            width = max(width, len(values))
            if not header_row:
                present = [v for v in values if v not in (None, "")]
                if len(present) >= 2 and len(set(map(str, present))) >= 2:
                    header_row, header = number, values
            # Numeric merged values are shown expanded, but counted only once.
            aggregate = list(values)
            for r1, c1, r2, c2, anchor, anchor_invalid in active:
                if isinstance(anchor, (int, float)) and not isinstance(anchor, bool):
                    for col in range(c1 - 1, c2):
                        if number != r1 or col != c1 - 1:
                            aggregate[col] = None
            record = {"v": values}
            if invalid:
                record["e"] = sorted(invalid)
            if aggregate != values:
                record["a"] = aggregate
            line = (json.dumps(record, ensure_ascii=False) + "\n").encode()
            written += len(line)
            if written > budget:
                raise SpreadsheetExtractError("DOCUMENT_RESULT_TOO_LARGE", "Structured expansion quota exceeded")
            handle.write(line)
    if not header_row:
        header_row, header = 1, first
    originals, names = _column_names(header, width)
    counts = [{"nulls": 0, "seen": 0, "bool": True, "int": True, "float": True, "date": True} for _ in names]
    filename = f"sheet_{index:02d}.rows.jsonl"
    offsets, blocks = [], []
    written = 0
    count = 0
    block_start, block_size, block_count = 1, 0, 0
    with spool.open("rb") as src, (target_dir / filename).open("wb") as dst:
        for position, line in enumerate(src, 1):
            if position <= header_row:
                continue
            record = json.loads(line)
            values = record["v"]
            values.extend([None] * (width - len(values)))
            if "a" in record:
                record["a"].extend([None] * (width - len(record["a"])))
            count += 1
            record["r"] = count
            if (count - 1) % BLOCK_ROWS == 0:
                offsets.append(written)
            encoded = (json.dumps(record, ensure_ascii=False) + "\n").encode()
            written += len(encoded)
            if written > budget:
                raise SpreadsheetExtractError("DOCUMENT_RESULT_TOO_LARGE", "Structured expansion quota exceeded")
            dst.write(encoded)
            for state, value in zip(counts, values):
                if value is None or value == "":
                    state["nulls"] += 1
                else:
                    state["seen"] += 1
                    state["bool"] &= isinstance(value, bool)
                    state["int"] &= isinstance(value, int) and not isinstance(value, bool)
                    state["float"] &= isinstance(value, (int, float)) and not isinstance(value, bool)
                    state["date"] &= _is_dateish(value)
            row_size = len(json.dumps(render_markdown([], [(count, values)]), ensure_ascii=False).encode())
            if block_count and (block_count >= CHUNK_SIM_ROWS or block_size + row_size > 10000):
                blocks.append([block_start, count - 1])
                block_start, block_count, block_size = count, 0, 0
            block_count += 1
            block_size += row_size
    spool.unlink()
    if block_count:
        blocks.append([block_start, count])
    profiles = []
    for i, (column, state) in enumerate(zip(names, counts)):
        dtype = next((key for key in ("bool", "int", "float", "date") if state["seen"] and state[key]), "str")
        profiles.append({"name": column, "dtype": dtype, "nulls": state["nulls"],
                         "source_index": i, "original_name": originals[i]})
    return {"name": name, "index": index, "rows": count, "cols": width,
            "header_row": header_row, "formula_count": quality["formula_count"],
            "formula_cache_status": "partial" if quality.get("invalid_formulas") else "available" if quality["formula_count"] else "not_applicable",
            "invalid_formula_count": quality.get("invalid_formulas", 0),
            "merged_ranges": merged, "columns": profiles, "file": filename,
            "block_rows": BLOCK_ROWS, "block_offsets": offsets or [0], "legacy_blocks": blocks}, written


def _column_names(header, width):
    originals = [str(header[i]).strip() if i < len(header) and header[i] is not None else "" for i in range(width)]
    reserved, used, names = set(filter(None, originals)), set(), []
    for i, original in enumerate(originals):
        base = original or f"col{i + 1}"
        name, suffix = base, 2
        while name in used or (not original and name in reserved):
            name = f"{base}#{suffix}"
            suffix += 1
            while name in reserved:
                name = f"{base}#{suffix}"
                suffix += 1
        used.add(name)
        names.append(name)
    return originals, names


def _stream_delimited(path, suffix):
    encoding = _text_encoding(path)
    with path.open("r", encoding=encoding, newline="") as stream:
        reader = csv.reader(stream, delimiter="\t" if suffix == ".tsv" else ",")
        yield "data", reader, [], {"formula_count": 0}


def _text_encoding(path):
    with path.open("rb") as stream:
        prefix = stream.read(4)
    encodings = (("utf-8-sig",) if prefix.startswith(codecs.BOM_UTF8) else
                 ("utf-16",) if prefix.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)) else
                 ("utf-8", "gb18030"))
    for encoding in encodings:
        try:
            decoder = codecs.getincrementaldecoder(encoding)()
            with path.open("rb") as stream:
                while chunk := stream.read(65536):
                    decoder.decode(chunk)
                decoder.decode(b"", final=True)
            return encoding
        except UnicodeDecodeError:
            continue
    raise SpreadsheetExtractError("DOCUMENT_TEXT_ENCODING_UNSUPPORTED", "Spreadsheet text encoding is unsupported")


def _stream_xlsx(path, temporary_root, allow_partial):
    from .workbook_reader import open_workbooks
    merged = _merged_ranges_from_xml(path)
    with open_workbooks(path, temporary_root) as (workbook, formulas):
        for index, sheet in enumerate(workbook.worksheets):
            formula_sheet = formulas.worksheets[index]
            sheet.reset_dimensions()
            formula_sheet.reset_dimensions()
            quality = {"formula_count": 0, "invalid_formulas": 0}
            def rows(sheet=sheet, formula_sheet=formula_sheet, quality=quality):
                for row, formula_row in zip(sheet.iter_rows(), formula_sheet.iter_rows(), strict=True):
                    invalid = []
                    for column, (value, formula) in enumerate(zip(row, formula_row, strict=True)):
                        if formula.data_type == "f":
                            quality["formula_count"] += 1
                            if value.value is None or value.data_type == "e":
                                if not allow_partial:
                                    raise SpreadsheetExtractError("DOCUMENT_FORMULA_CACHE_MISSING", "Recalculate and save the workbook in Excel before uploading again")
                                invalid.append(column)
                                quality["invalid_formulas"] += 1
                    yield SourceRow([_cell(value.value) for value in row], invalid)
            yield str(sheet.title), rows(), merged.get(str(sheet.title), []), quality


def _write_rows(target: Path, rows: Iterable[tuple[int, list[Any]]]) -> list[int]:
    offsets: list[int] = []
    written = 0
    with target.open("wb") as handle:
        for position, (row_number, values) in enumerate(rows):
            if position % BLOCK_ROWS == 0:
                offsets.append(written)
            line = (
                json.dumps({"r": row_number, "v": values}, ensure_ascii=False) + "\n"
            ).encode("utf-8")
            handle.write(line)
            written += len(line)
    if not offsets:
        offsets.append(0)
    return offsets


def _cell(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (date, time)):
        return value.isoformat()
    text = str(value)
    return text


def _is_dateish(value: Any) -> bool:
    return isinstance(value, str) and len(value) >= 8 and value[:4].isdigit()


def _merged_ranges_from_xml(path: Path) -> dict[str, list[list[int]]]:
    """Read mergeCell metadata straight from OOXML (read-only mode lacks it)."""
    import re
    import zipfile
    from xml.etree import ElementTree

    namespaces = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    result: dict[str, list[list[int]]] = {}
    try:
        with zipfile.ZipFile(path) as archive:
            workbook_xml = ElementTree.fromstring(archive.read("xl/workbook.xml"))
            rels = ElementTree.fromstring(
                archive.read("xl/_rels/workbook.xml.rels")
            )
            rel_targets = {
                rel.get("Id"): rel.get("Target") for rel in rels
            }
            for sheet in workbook_xml.findall("main:sheets/main:sheet", namespaces):
                name = str(sheet.get("name") or "")
                target = rel_targets.get(sheet.get(
                    "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
                ), "")
                if not target:
                    continue
                member = target if target.startswith("xl/") else "xl/" + target.lstrip("/")
                if target.startswith("/"):
                    member = target.lstrip("/")
                ranges = []
                with archive.open(member) as xml:
                    stack = []
                    for event, element in ElementTree.iterparse(xml, events=("start", "end")):
                        if event == "start":
                            stack.append(element)
                            continue
                        if element.tag.endswith("}mergeCell"):
                            ref = str(element.get("ref") or "")
                            match = re.fullmatch(r"([A-Z]+)(\d+):([A-Z]+)(\d+)", ref)
                            if match:
                                ranges.append([int(match.group(2)), _col_index(match.group(1)),
                                               int(match.group(4)), _col_index(match.group(3))])
                        element.clear()
                        stack.pop()
                        if stack:
                            stack[-1].remove(element)
                result[name] = ranges
    except (OSError, zipfile.BadZipFile, ElementTree.ParseError):
        return {}
    return result


def _col_index(label: str) -> int:
    index = 0
    for char in label:
        index = index * 26 + (ord(char) - 64)
    return index


def render_markdown(columns: list[str], rows: Iterable[tuple[int, list[Any]]]) -> str:
    lines = ["| " + " | ".join(columns) + " |", "| " + " | ".join(["---"] * len(columns)) + " |"]
    for _, values in rows:
        cells = ["" if value is None else str(value) for value in values]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


__all__ = [
    "BLOCK_ROWS",
    "CHUNK_SIM_ROWS",
    "SpreadsheetExtractError",
    "extract_workbook",
    "render_markdown",
]
