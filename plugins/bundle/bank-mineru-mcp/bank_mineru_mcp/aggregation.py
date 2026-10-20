"""Exact disk-backed aggregation for validated spreadsheet queries."""
from decimal import Decimal, InvalidOperation
import json
import sqlite3
import tempfile
import os
from pathlib import Path
from .spreadsheet import SpreadsheetExtractError
from .table_semantics import numeric_value, decimal_result, decimal_add, decimal_average, ENGINE_VERSION


class DecimalSum:
    def __init__(self):
        self.total = Decimal(0)
    def step(self, value):
        if value is not None:
            self.total = decimal_add(self.total, Decimal(value))
    def finalize(self):
        return str(self.total)


def disk_aggregate(rows, *, names, group_by, metrics, filters, match, directory,
                   max_bytes, max_groups, group_cursor=None, response_bytes=16000,
                   numeric_text="strict"):
    columns = list(dict.fromkeys(m['column'] for m in metrics))
    positions = {name: i for i, name in enumerate(names)}
    matched = 0
    with tempfile.TemporaryDirectory(prefix='.aggregate-', dir=os.environ.get('BANK_READING_WORK_ROOT') or directory) as temporary:
        db = sqlite3.connect(Path(temporary) / 'query.sqlite')
        try:
            db.execute('PRAGMA journal_mode=OFF')
            db.execute('PRAGMA cache_size=-4096')
            db.execute('PRAGMA temp_store=FILE')
            db.execute(f'PRAGMA max_page_count={max(64, max_bytes // 4096)}')
            db.create_aggregate('decimal_sum', 1, DecimalSum)
            db.create_collation('DECIMAL', lambda a, b: (Decimal(a) > Decimal(b)) - (Decimal(a) < Decimal(b)))
            db.execute('CREATE TABLE vals (g TEXT, c INTEGER, v TEXT, n TEXT, bad INTEGER)')
            for _, values in rows:
                def get(name):
                    i = positions[name]
                    if i in getattr(values, "invalid_columns", ()):
                        raise SpreadsheetExtractError("DOCUMENT_FORMULA_CACHE_MISSING", "The requested calculation depends on invalid formula caches")
                    return values[i] if i < len(values) else None
                if filters and not match(get(filters['column']), filters['op'], filters.get('value')):
                    continue
                matched += 1
                key = json.dumps([get(n) for n in group_by], ensure_ascii=False)
                batch = []
                for column_index, column in enumerate(columns):
                    value = get(column)
                    present = value is not None and value != ''
                    numeric, bad = None, 0
                    if present:
                        number = numeric_value(value, numeric_text=numeric_text)
                        if number is not None:
                            numeric = str(number)
                        else:
                            bad = 1
                    batch.append((key, column_index, json.dumps(value, ensure_ascii=False, sort_keys=True) if present else None, numeric, bad))
                db.executemany('INSERT INTO vals VALUES (?,?,?,?,?)', batch)
            db.execute('CREATE INDEX by_group_column ON vals(g,c)')
            count = db.execute('SELECT COUNT(DISTINCT g) FROM vals').fetchone()[0]
            if group_cursor is None and count > max_groups:
                raise OverflowError('Use group_cursor=0 to page a high-cardinality aggregate')
            offset = group_cursor or 0
            keys = db.execute('SELECT DISTINCT g FROM vals ORDER BY g LIMIT ? OFFSET ?', (max_groups, offset)).fetchall()
            output = []
            for (key,) in keys:
                item = {'group': dict(zip(group_by, json.loads(key)))} if group_by else {}
                exact_values = {}
                for metric in metrics:
                    column, fn = metric['column'], metric['fn']
                    args = (key, columns.index(column))
                    total, present, invalid = db.execute('SELECT COUNT(*),COUNT(n),COALESCE(SUM(bad),0) FROM vals WHERE g=? AND c=?', args).fetchone()
                    result = None
                    exact = None
                    if fn in {'count', 'count_rows'}:
                        result = total
                    elif fn == 'count_nonempty':
                        result = db.execute('SELECT COUNT(v) FROM vals WHERE g=? AND c=?', args).fetchone()[0]
                    elif fn == 'count_numeric':
                        result = present
                    elif fn == 'count_distinct':
                        result = db.execute('SELECT COUNT(DISTINCT v) FROM vals WHERE g=? AND c=?', args).fetchone()[0]
                    elif present and not invalid:
                        if fn in {'sum', 'avg'}:
                            amount = Decimal(db.execute('SELECT decimal_sum(n) FROM vals WHERE g=? AND c=?', args).fetchone()[0])
                            result, exact = decimal_result(decimal_average(amount, present) if fn == 'avg' else amount)
                        elif fn in {'min', 'max'}:
                            direction = 'ASC' if fn == 'min' else 'DESC'
                            value = db.execute('SELECT n FROM vals WHERE g=? AND c=? AND n IS NOT NULL ORDER BY n COLLATE DECIMAL ' + direction + ' LIMIT 1', args).fetchone()[0]
                            result, exact = decimal_result(Decimal(value))
                        elif fn == 'median':
                            values = db.execute('SELECT n FROM vals WHERE g=? AND c=? AND n IS NOT NULL ORDER BY n COLLATE DECIMAL LIMIT ? OFFSET ?', (*args, 2 if present % 2 == 0 else 1, (present - 1) // 2)).fetchall()
                            amount = Decimal(0)
                            for value in values:
                                amount = decimal_add(amount, Decimal(value[0]))
                            result, exact = decimal_result(decimal_average(amount, len(values)))
                    item[f'{column}:{fn}'] = result
                    if exact is not None:
                        exact_values[f'{column}:{fn}'] = exact
                if exact_values:
                    item['exact_values'] = exact_values
                output.append(item)
            if group_cursor is not None:
                from .inventory import encoded_size
                while len(output) > 1 and encoded_size(output) > response_bytes:
                    output.pop()
            result = {'groups': output, 'group_count': count, 'rows_matched': matched,
                      'metrics': metrics, 'group_by': group_by,
                      'semantics': {'engine': ENGINE_VERSION, 'values': 'raw_anchor',
                                    'numeric_text': numeric_text, 'count': 'count_rows',
                                    'decimal_precision': 128, 'union': 'append'}}
            if group_cursor is not None:
                result['next_group_cursor'] = offset + len(output) if offset + len(output) < count else None
                result['groups_complete'] = offset == 0 and len(output) == count
            return result
        finally:
            db.close()
