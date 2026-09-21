import json
from mcp.types import CallToolResult, TextContent
from qwenpaw.drivers.adapters.agentscope_tool import _blocks_from_value


def test_identical_structured_payload_is_emitted_once():
    data = {"items": [{"text": "中文" * 10000}]}
    result = CallToolResult(content=[TextContent(type="text", text=json.dumps(data))], structuredContent=data)
    blocks = _blocks_from_value(result)
    assert len(blocks) == 1
    assert json.loads(blocks[0].text) == data


def test_distinct_text_and_structured_payload_are_preserved():
    result = CallToolResult(content=[TextContent(type="text", text="explanation")], structuredContent={"ok": True})
    assert len(_blocks_from_value(result)) == 2
