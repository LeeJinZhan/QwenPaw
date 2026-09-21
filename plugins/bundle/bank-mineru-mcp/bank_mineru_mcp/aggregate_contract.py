"""Aggregate schema and fixed, non-sensitive argument diagnostics.

Schema is descriptive at the MCP boundary: validate original arguments only
AFTER the one-use authorization digest has been consumed. Never coerce or add
nested defaults before that boundary.
"""
FUNCTIONS = ['sum', 'avg', 'count', 'count_distinct', 'min', 'max', 'median']
FILTERS = ['eq', 'ne', 'gt', 'gte', 'lt', 'lte', 'contains', 'in']
_DETAILS = {
    'PARSE_DOCUMENTS': ('documents', 'documents 为 1–5 个对象，每项使用当前附件的 file_id；银行 Gateway 可补全省略的 file_ref。不要增加路径或 URL 字段。'),
    'PARSE_METHOD': ('parse_method', 'parse_method 仅支持 auto、ocr、txt；一般保留默认 auto。'),
    'PARSE_LANGUAGE': ('language', 'language 仅支持 auto、zh、en；一般保留默认 auto。'),
    'PARSE_OPTIONS': ('options', 'options 仅使用 tables、formulas 布尔值；不确定时省略 options。'),
    'OPS': ('ops', 'ops 必须包含 1–10 个统计操作对象。'),
    'OP_FIELDS': ('ops[]', '只使用 sheet、metrics、group_by、filter、row_range、group_cursor、cross_sheet_union 字段。'),
    'METRICS': ('ops[].metrics', 'metrics 必须是非空对象数组，每项指定 column 和 fn。'),
    'METRIC_FUNCTION_FIELD': ('ops[].metrics[].fn', '统计函数字段是 fn，不是 op。例如 {"column":"实际列名","fn":"count"}；仅 filter 使用 op。'),
    'METRIC_FUNCTION': ('ops[].metrics[].fn', 'fn 仅支持 sum、avg、count、count_distinct、min、max、median。'),
    'METRIC_COLUMN': ('ops[].metrics[].column', 'column 必须精确复制该工作表 inventory 的列名；count 也需要有效列名，不接受 *。'),
    'METRIC_FIELDS': ('ops[].metrics[]', '每个 metric 只使用 column 和 fn，不使用别名字段。'),
    'GROUP_BY': ('ops[].group_by', 'group_by 必须是不重复的已有列名数组；列名从该工作表 inventory 复制。'),
    'FILTER': ('ops[].filter', 'filter 使用 {"column":"实际列名","op":"eq","value":"筛选值"}；op 为 eq/ne/gt/gte/lt/lte/contains/in，in 的 value 必须是数组。'),
    'ROW_RANGE': ('ops[].row_range', 'row_range 必须为 [起始行,结束行]，两项均为正整数，起点不大于终点。'),
    'GROUP_CURSOR': ('ops[].group_cursor', 'group_cursor 为非负整数；第一页传 0，后续复制 next_group_cursor。'),
    'SHEET': ('ops[].sheet', '多工作表必须指定 sheet；使用 inventory 中的精确工作表名，不根据正文猜测。'),
    'UNION': ('ops[].cross_sheet_union', '跨表统计使用 {"key_column":"实际公共列名"}；不可同时指定单个 sheet 或 row_range。'),
    'UNION_COLUMNS': ('ops[].cross_sheet_union', '参与跨表统计的每张表都必须包含所需指标、分组、筛选列；先读取各表 inventory 核对，不猜测列映射。'),
}
_MESSAGES = {
    'documents must contain 1-5 files':'PARSE_DOCUMENTS',
    'document fields are invalid':'PARSE_DOCUMENTS',
    'parse_method is invalid':'PARSE_METHOD',
    'language is invalid':'PARSE_LANGUAGE',
    'options are invalid':'PARSE_OPTIONS',
    'ops must contain 1-10 operations':'OPS',
    'aggregate group_by is invalid':'GROUP_BY',
    'aggregate metrics are required':'METRICS',
    'aggregate filter is invalid':'FILTER',
    'group_cursor must be nonnegative':'GROUP_CURSOR',
    'row range must be ordered positive integers':'ROW_RANGE',
    'sheet parameter is required for multi-sheet workbooks':'SHEET',
    'sheet is not in this workbook':'SHEET',
    'cross_sheet_union is invalid':'UNION',
    'union cannot also select sheet or row_range':'UNION',
    'Union metrics or groups are invalid':'METRICS',
    'Union source lacks a required column':'UNION_COLUMNS',
    'union key column is absent':'UNION',
}


def argument_detail(reason):
    """Only trusted catalog text is ever returned to callers or logs."""
    reason = _MESSAGES.get(reason, reason) if isinstance(reason, str) else ''
    if reason not in _DETAILS:
        return {}
    field, hint = _DETAILS[reason]
    return {'reason':reason, 'field':field, 'hint':hint}


_COLUMN = {'type':'string', 'minLength':1, 'description':'Exact column name from this sheet inventory; count also requires a column.'}
AGGREGATE_OPS_SCHEMA = {
    'type':'array', 'minItems':1, 'maxItems':10,
    'items':{
        'type':'object', 'additionalProperties':False, 'required':['metrics'],
        'properties':{
            'sheet':{'type':'string', 'description':'Exact inventory sheet name; required for multi-sheet workbooks unless using cross_sheet_union.'},
            'metrics':{'type':'array', 'minItems':1, 'items':{
                'type':'object', 'additionalProperties':False, 'required':['column','fn'],
                'properties':{'column':_COLUMN, 'fn':{'type':'string', 'enum':FUNCTIONS, 'description':'Use fn, NOT op; count counts matching rows.'}}}},
            'group_by':{'type':'array', 'uniqueItems':True, 'items':_COLUMN},
            'filter':{'type':'object', 'additionalProperties':False, 'required':['column','op','value'],
                'properties':{'column':_COLUMN,'op':{'type':'string','enum':FILTERS},
                              'value':{'description':'Comparison value; an array is required for in.'}}},
            'row_range':{'type':'array','minItems':2,'maxItems':2,'items':{'type':'integer','minimum':1}},
            'group_cursor':{'type':'integer','minimum':0},
            'cross_sheet_union':{'type':'object','additionalProperties':False,'required':['key_column'],
                'properties':{'key_column':_COLUMN}},
        },
    },
}


def invalid_ops(ops):
    """Validate shape without modifying arguments or guessing column names."""
    if not isinstance(ops, list) or not 1 <= len(ops) <= 10 or not all(isinstance(op, dict) for op in ops):
        return 'OPS'
    for op in ops:
        if set(op) - AGGREGATE_OPS_SCHEMA['items']['properties'].keys():
            return 'OP_FIELDS'
        if op.get('sheet') is not None and not isinstance(op['sheet'], str):
            return 'SHEET'
        metrics = op.get('metrics')
        if not isinstance(metrics,list) or not metrics or not all(isinstance(m,dict) for m in metrics):
            return 'METRICS'
        for metric in metrics:
            if 'op' in metric or 'fn' not in metric:
                return 'METRIC_FUNCTION_FIELD'
            if not isinstance(metric['fn'],str) or metric['fn'] not in FUNCTIONS:
                return 'METRIC_FUNCTION'
            if not isinstance(metric.get('column'),str) or not metric['column']:
                return 'METRIC_COLUMN'
            if set(metric) - {'column','fn'}:
                return 'METRIC_FIELDS'
        groups = op.get('group_by')
        if groups is not None and (not isinstance(groups,list) or not all(isinstance(g,str) for g in groups) or len(groups)!=len(set(groups))):
            return 'GROUP_BY'
        filters = op.get('filter')
        if filters is not None and filters != {}:
            if (not isinstance(filters,dict) or set(filters)!={'column','op','value'}
                or not isinstance(filters['column'],str) or not isinstance(filters['op'],str)
                or filters['op'] not in FILTERS or (filters['op']=='in' and not isinstance(filters['value'],list))):
                return 'FILTER'
        rows = op.get('row_range')
        if rows is not None and (not isinstance(rows,(list,tuple)) or len(rows)!=2 or any(type(r) is not int or r<1 for r in rows) or rows[0]>rows[1]):
            return 'ROW_RANGE'
        cursor = op.get('group_cursor')
        if cursor is not None and (type(cursor) is not int or cursor<0):
            return 'GROUP_CURSOR'
        union = op.get('cross_sheet_union')
        if union is not None and union != {}:
            if (not isinstance(union,dict) or set(union)!={'key_column'} or not isinstance(union['key_column'],str)
                or not union['key_column'] or rows is not None or op.get('sheet') not in (None,'','*')):
                return 'UNION'
    return ''
