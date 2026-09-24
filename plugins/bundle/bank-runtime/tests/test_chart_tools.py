import json
import pytest
from bank_runtime.chart_tools import chart_generate, chart_model_result
from bank_runtime.gateway.middleware import _runtime_tool_response
from bank_runtime.gateway.completion import operation_keys


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
