"""Byte-bounded metadata views; persisted metadata is never truncated."""
from __future__ import annotations
import json


def encoded_size(value):
    return len(json.dumps(value, ensure_ascii=False, indent=2).encode('utf-8'))


def inventory_summary(inventory, *, start=0, budget=4200):
    result = {k: inventory[k] for k in ('engine', 'title', 'sheet_count', 'total_rows')}
    result.update({k: inventory[k] for k in ('value_semantics', 'source_format', 'excluded') if k in inventory})
    result.update(sheets=[], inventory_complete=False, next_inventory_cursor=start)
    for position in range(start, len(inventory['sheets'])):
        sheet = inventory['sheets'][position]
        item = {k: sheet[k] for k in ('name', 'index', 'rows', 'cols', 'header_row', 'formula_count', 'formula_cache_status')}
        item.update({k: sheet[k] for k in ('source_rows', 'header_status', 'hidden', 'formula_expression_status', 'formula_detection_status') if k in sheet})
        # Columns are a convenience in the first page; the explicit metadata
        # reader always exposes all columns and merge ranges independently.
        item['columns'] = sheet['columns'] if encoded_size(sheet['columns']) < 1800 else []
        item['columns_complete'] = len(item['columns']) == len(sheet['columns'])
        item['merged_range_count'] = len(sheet['merged_ranges'])
        item['hidden_row_count'] = len(sheet.get('hidden_rows', []))
        item['hidden_column_count'] = len(sheet.get('hidden_columns', []))
        result['sheets'].append(item)
        if encoded_size(result) > budget:
            item['columns'] = []
            item['columns_complete'] = not sheet['columns']
        if encoded_size(result) > budget and len(result['sheets']) > 1:
            result['sheets'].pop()
            break
        result['next_inventory_cursor'] = position + 1
    result['inventory_complete'] = result['next_inventory_cursor'] >= inventory['sheet_count']
    if result['inventory_complete']:
        result['next_inventory_cursor'] = None
    return result


def inventory_page(inventory, *, sheet=None, start=0):
    if sheet is None:
        return {'inventory': inventory_summary(inventory, start=start, budget=24000)}
    meta = next((s for s in inventory['sheets'] if s['name'] == sheet), None)
    if meta is None:
        raise ValueError('sheet is not in this workbook')
    column_count, merge_count = len(meta['columns']), len(meta['merged_ranges'])
    hidden_rows, hidden_columns = meta.get('hidden_rows', []), meta.get('hidden_columns', [])
    count = column_count + merge_count + len(hidden_rows) + len(hidden_columns)
    result = {'sheet': sheet, 'metadata': [], 'next_inventory_cursor': None, 'inventory_complete': True}
    for index in range(start, count):
        if index < column_count:
            entry = {'kind': 'column', **meta['columns'][index]}
        elif index < column_count + merge_count:
            entry = {'kind': 'merge', 'range': meta['merged_ranges'][index - column_count]}
        elif index < column_count + merge_count + len(hidden_rows):
            entry = {'kind': 'hidden_row', 'source_row': hidden_rows[index - column_count - merge_count]}
        else:
            entry = {'kind': 'hidden_column', 'source_column': hidden_columns[index - column_count - merge_count - len(hidden_rows)]}
        result['metadata'].append(entry)
        if encoded_size(result) > 24000:
            result['metadata'].pop()
            if not result['metadata']:
                raise ValueError('A column name exceeds the metadata budget')
            result.update(next_inventory_cursor=index, inventory_complete=False)
            break
    return result
