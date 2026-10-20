import json
import re
from pathlib import Path
import pytest
from bank_runtime.chart_tools import chart_generate, chart_model_result
from bank_runtime.gateway.middleware import _runtime_tool_response
from bank_runtime.gateway.completion import operation_keys
from bank_runtime.gateway.middleware import BankRuntimeGatewayMiddleware


@pytest.mark.asyncio
async def test_chart_tool_has_no_local_execution_fallback():
    with pytest.raises(RuntimeError, match='Tool Gateway'):
        await chart_generate({'schema_version': 'chart/1'})


def test_success_requires_persisted_chart_and_version_identifiers():
    for result in [{}, {'chart_status': 'ready'}, {'chart_status': 'ready', 'chart_id': 'chart'}]:
        assert chart_model_result({'status': 'success', 'result': result})['presentation']['outcome'] == 'unknown'
    result = chart_model_result({'status': 'success', 'result': {'chart_status': 'ready', 'chart_id': 'chart', 'version_id': 'v1', 'object_key': 'private'}})
    assert result['presentation']['outcome'] == 'completed'
    assert 'private' not in json.dumps(result)


def test_chart_failure_is_not_described_as_a_generated_file():
    response = _runtime_tool_response('call', {'status': 'error', 'result': {}}, tool_name='chart_generate')
    text = response.content[0].text
    assert '图表' in text and '文件已生成' not in text


def test_unknown_chart_write_is_tracked_for_completion_guard():
    assert operation_keys('chart_generate', {'definition': {'title': '关系图'}})


@pytest.mark.asyncio
async def test_chart_export_requires_gateway_and_saved_version():
    from bank_runtime import chart_tools
    assert hasattr(chart_tools, 'chart_export')
    with pytest.raises(RuntimeError, match='Tool Gateway'):
        await chart_tools.chart_export('chart', 'png', 'version')
    assert operation_keys('chart_export', {'chart_id': 'chart', 'version_id': 'version', 'format': 'png'})


def test_chart_export_result_requires_actual_requested_format_evidence():
    result = {'chart_id': 'chart', 'version_id': 'version', 'format': 'png',
              'artifact_status': 'succeeded', 'generated_file_ids': ['file'], 'mime_type': 'image/png'}
    response = _runtime_tool_response('call', {'status': 'success', 'result': result}, tool_name='chart_export')
    value = json.loads(response.content[0].text)
    assert value['presentation']['outcome'] == 'completed'
    assert value['result']['version_id'] == 'version'
    assert value['result']['format'] == 'png'
    response = _runtime_tool_response('call', {'status': 'failed', 'error_code': 'WORKER_TIMEOUT', 'result': {}}, tool_name='chart_export')
    value = json.loads(response.content[0].text)
    assert value['result']['retryable'] is False
    assert '格式' in value['presentation']['message']


@pytest.mark.asyncio
async def test_export_gateway_success_does_not_clear_another_failed_format():
    from agentscope.message import ToolCallBlock
    from bank_runtime.artifact_tools import FileOperationsIncompleteError
    from test_gateway_middleware import _Client

    class Client(_Client):
        async def execute_runtime_tool(self, preflight, name, payload):
            await super().execute_runtime_tool(preflight, name, payload)
            if payload['format'] == 'png':
                return {'status': 'failed', 'error_code': 'WORKER_TIMEOUT', 'result': {}}
            return {'status': 'success', 'result': {'chart_id': 'chart', 'version_id': 'saved',
                    'format': 'svg', 'artifact_status': 'succeeded', 'generated_file_ids': ['svg-file']}}

    middleware = BankRuntimeGatewayMiddleware(Client())
    async def local_execution(**kwargs):
        raise AssertionError('chart export must execute through Runtime')
        yield

    for format in ('png', 'svg'):
        payload = {'chart_id': 'chart', 'format': format}
        middleware.prepare('chart_export', payload, {'tool_call_id': format})
        call = ToolCallBlock(id=format, name='chart_export', input=json.dumps(payload))
        result = [item async for item in middleware.on_acting(None, {'tool_call': call}, local_execution)]
        assert json.loads(result[-1].content[0].text)['presentation']['outcome'] == ('unknown' if format == 'png' else 'completed')
    assert middleware.unresolved_file_operations == operation_keys('chart_export', {'chart_id': 'chart', 'format': 'png'})
    with pytest.raises(FileOperationsIncompleteError):
        middleware._check_file_completion()


def test_render_resource_failure_does_not_advise_changing_content_types():
    from bank_runtime.presentation import artifact_model_result
    for code in ('WORKER_TIMEOUT', 'WORKER_UNAVAILABLE'):
        result = artifact_model_result({'status': 'failed', 'error_code': code, 'result': {}})
        assert result['result']['retryable'] is False
        assert result['result']['execution_status'] == 'execution_unknown'
        assert result['result']['recovery_action'] == 'check_status'
        assert '数值类型' not in result['presentation']['message']


def test_chart_skill_examples_cover_unlabelled_edges_and_missing_line_values():
    skill = Path(__file__).parents[1] / 'skills/bank-chart/SKILL.md'
    examples = [json.loads(block) for block in re.findall(r'```json\s*(.*?)\s*```', skill.read_text(encoding='utf-8'), re.S)]
    definitions = [example['definition'] for example in examples]
    assert any('label' not in edge for definition in definitions for edge in definition.get('edges', []))
    line = next(definition for definition in definitions if definition.get('chart_type') == 'line')
    assert line['axis_labels'] is True
    assert line['legend'] is True
    assert line['missing_values'] == 'gap'
    assert any(None in series['values'] for series in line['series'])
    for definition in definitions:
        for edge in definition.get('edges', []):
            assert edge.get('label') != ''
        for series in definition.get('series', []):
            assert len(series['values']) == len(definition['categories'])
            assert all(value is None or isinstance(value, str) for value in series['values'])


@pytest.mark.asyncio
async def test_first_model_call_sees_typed_chart_fields_without_changing_tool_admission():
    import copy
    tools = [{'type': 'function', 'function': {'name': 'chart_generate', 'parameters': {
        'type': 'object', 'properties': {'definition': {'type': 'object'}}, 'required': ['definition'],
    }}}, {'type': 'function', 'function': {'name': 'other', 'parameters': {'type': 'object'}}}]
    original = copy.deepcopy(tools)
    received = []
    async def model(**kwargs):
        received.append(kwargs)
        return object()
    await BankRuntimeGatewayMiddleware(None).on_model_call(None, {'tools': tools}, model)
    visible = received[0]['tools']
    fields = visible[0]['function']['parameters']['properties']['definition']['properties']
    assert fields['series']['items']['required'] == ['id', 'name', 'values']
    assert fields['axis_labels']['type'] == 'boolean'
    assert fields['series']['items']['properties']['values']['items']['type'] == ['string', 'null']
    assert fields['edges']['items']['properties']['label']['minLength'] == 1
    assert 'label' not in fields['edges']['items']['required']
    assert fields['nodes']['items']['properties']['shape']['enum'] == ['rectangle', 'rounded', 'ellipse', 'diamond', 'text']
    assert tools == original
    assert visible[1] == original[1]
    received.clear()
    await BankRuntimeGatewayMiddleware(None).on_model_call(None, {'tools': [tools[1]]}, model)
    assert received[0]['tools'] == [tools[1]]


def test_visible_schema_keeps_kind_requirements_and_rejects_common_format_errors():
    from copy import deepcopy
    from jsonschema import Draft202012Validator, ValidationError
    from bank_runtime.chart_schema import describe_chart_tools
    tools = [{'type': 'function', 'function': {'name': 'chart_generate'}}]
    schema = describe_chart_tools(tools)[0]['function']['parameters']['properties']['definition']
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    skill = Path(__file__).parents[1] / 'skills/bank-chart/SKILL.md'
    examples = [json.loads(block)['definition'] for block in re.findall(r'```json\s*(.*?)\s*```', skill.read_text(encoding='utf-8'), re.S)]
    for definition in examples:
        validator.validate(definition)
    line = next(d for d in examples if d['kind'] == 'statistical')
    for field, value in [('axis_labels', {'x': '月份'}), ('nodes', []), ('chart_type', 'unsupported')]:
        invalid = {**line, field: value}
        with pytest.raises(ValidationError):
            validator.validate(invalid)
    invalid = deepcopy(line)
    del invalid['series'][0]['id']
    with pytest.raises(ValidationError):
        validator.validate(invalid)
    swimlane = deepcopy(next(d for d in examples if d['kind'] == 'swimlane'))
    del swimlane['lanes']
    with pytest.raises(ValidationError):
        validator.validate(swimlane)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['swimlane', 'statistical'])
async def test_chart_format_is_normalized_before_preflight_and_execution(kind):
    from copy import deepcopy
    from types import SimpleNamespace
    from agentscope.message import ToolCallBlock, ToolResultState
    from agentscope.permission import PermissionBehavior
    from bank_runtime.gateway.middleware import GatewayPermissionEngine
    from test_gateway_middleware import _Client, _DelegateEngine
    from jsonschema import Draft7Validator

    skill = Path(__file__).parents[1] / 'skills/bank-chart/SKILL.md'
    definition = next(json.loads(block)['definition'] for block in re.findall(
        r'```json\s*(.*?)\s*```', skill.read_text(encoding='utf-8'), re.S)
        if json.loads(block)['definition']['kind'] == kind)
    if kind == 'swimlane':
        definition['edges'][0]['label'] = ''
    else:
        definition['series'][0]['values'][1] = 'null'
    raw = {'definition': definition, 'source_refs': []}
    original = deepcopy(raw)
    client = _Client()
    middleware = BankRuntimeGatewayMiddleware(client)
    engine = GatewayPermissionEngine(_DelegateEngine(PermissionBehavior.ALLOW, client.events), middleware)
    decision = await engine.check_permission(SimpleNamespace(name='chart_generate', is_external_tool=False), raw)
    assert decision.behavior == PermissionBehavior.ALLOW
    admitted = client.events[0][2]
    expected = deepcopy(original)
    if kind == 'swimlane':
        del expected['definition']['edges'][0]['label']
    else:
        expected['definition']['series'][0]['values'][1] = None
    assert admitted == expected
    assert raw == original
    schema = json.loads((Path(__file__).parents[1] / 'bank_runtime/chart_definition.schema.json').read_text())
    assert list(Draft7Validator(schema).iter_errors(original['definition']))
    Draft7Validator(schema).validate(admitted['definition'])

    async def local_execution(**kwargs):
        raise AssertionError('chart must execute through Runtime')
        yield

    call = ToolCallBlock(id='chart-call', name='chart_generate', input=json.dumps(raw))
    output = [item async for item in middleware.on_acting(None, {'tool_call': call}, local_execution)]
    assert output[-1].state == ToolResultState.SUCCESS
    executed = next(event[3] for event in client.events if event[0] == 'runtime_execute')
    assert executed == admitted
    assert json.loads(call.input) == original
    assert len([event for event in client.events if event[0] == 'preflight']) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('tool_name,definition', [
    ('other', {'edges': [{'label': ''}]}),
    ('chart_generate', {'nodes': [{'label': ''}], 'lanes': [{'label': ''}],
                        'participants': [{'label': ''}], 'messages': [{'label': ''}]}),
    ('chart_generate', {'edges': [{'label': ' '}, {'label': None}, {'label': 0},
                                  {'label': False}, {'label': []}, {'label': '否'}, {}]}),
    ('chart_generate', {'edges': {'label': ''}}),
    ('chart_generate', {'edges': [None, '', {'label': ''}]}),
    ('chart_generate', None),
    ('other', {'kind': 'statistical', 'series': [{'values': ['null']}]}),
    ('chart_generate', {'kind': 'flow', 'series': [{'values': ['null']}]}),
    ('chart_generate', {'series': [{'values': ['null']}]}),
    ('chart_generate', {'kind': 'statistical', 'series': [{'values': ['', 'NULL', 'NaN',
        'missing', ' null', 'null ', 0, False, None, '1.00']}]}),
    ('chart_generate', {'kind': 'statistical', 'series': {'values': ['null']}}),
    ('chart_generate', {'kind': 'statistical', 'series': [{'values': 'null'}]}),
    ('chart_generate', {'kind': 'statistical', 'series': [None, {'values': ['null']}]}),
    ('chart_generate', {'kind': 'statistical', 'series': [{'values': ['null']}, {'values': {}}]}),
])
async def test_chart_normalization_preserves_invalid_shapes_and_business_labels(tool_name, definition):
    from copy import deepcopy
    from types import SimpleNamespace
    from agentscope.permission import PermissionBehavior
    from bank_runtime.gateway.middleware import GatewayPermissionEngine
    from test_gateway_middleware import _Client, _DelegateEngine

    raw = {'definition': definition}
    original = deepcopy(raw)
    client = _Client()
    engine = GatewayPermissionEngine(_DelegateEngine(PermissionBehavior.ALLOW, client.events),
                                     BankRuntimeGatewayMiddleware(client))
    await engine.check_permission(SimpleNamespace(name=tool_name, is_external_tool=False), raw)
    assert client.events[0][2] == original
    assert raw == original
