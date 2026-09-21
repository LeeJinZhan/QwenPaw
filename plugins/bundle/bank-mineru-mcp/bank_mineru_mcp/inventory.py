"""Byte-bounded metadata views; persisted metadata is never truncated."""
from __future__ import annotations
import json


def encoded_size(value):
    return len(json.dumps(value, ensure_ascii=False, indent=2).encode('utf-8'))


def inventory_summary(inventory, *, start=0, budget=4200):
    result = {k: inventory[k] for k in ('engine', 'title', 'sheet_count', 'total_rows')}
    result.update(sheets=[], inventory_complete=False, next_inventory_cursor=start)
    for position in range(start, len(inventory['sheets'])):
        sheet = inventory['sheets'][position]
        item = {k: sheet[k] for k in ('name', 'index', 'rows', 'cols', 'header_row', 'formula_count', 'formula_cache_status')}
        # Columns are a convenience in the first page; the explicit metadata
        # reader always exposes all columns and merge ranges independently.
        item['columns'] = sheet['columns'] if encoded_size(sheet['columns']) < 1800 else []
        item['columns_complete'] = len(item['columns']) == len(sheet['columns'])
        item['merged_range_count'] = len(sheet['merged_ranges'])
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
    count = len(meta['columns']) + len(meta['merged_ranges'])
    result = {'sheet': sheet, 'metadata': [], 'next_inventory_cursor': None, 'inventory_complete': True}
    for index in range(start, count):
        entry = ({'kind': 'column', **meta['columns'][index]} if index < len(meta['columns']) else
                 {'kind': 'merge', 'range': meta['merged_ranges'][index - len(meta['columns'])]})
        result['metadata'].append(entry)
        if encoded_size(result) > 24000:
            result['metadata'].pop()
            if not result['metadata']:
                raise ValueError('A column name exceeds the metadata budget')
            result.update(next_inventory_cursor=index, inventory_complete=False)
            break
    return result
