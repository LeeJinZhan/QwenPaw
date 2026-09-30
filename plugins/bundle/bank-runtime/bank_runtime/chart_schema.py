"""Model-visible fields copied from chart/1; Runtime remains validation authority."""

from copy import deepcopy
from functools import lru_cache
import json
from pathlib import Path


@lru_cache(maxsize=1)
def _definition_schema():
    source = json.loads(Path(__file__).with_name("chart_definition.schema.json").read_text(encoding="utf-8"))
    definitions = source["$defs"]

    def inline(value):
        if isinstance(value, list):
            return [inline(item) for item in value]
        if not isinstance(value, dict):
            return value
        if "$ref" in value:
            # Only packaged local definitions, never URLs or model input.
            reference = value["$ref"]
            if not reference.startswith("#/$defs/"):
                raise ValueError("unsupported_chart_schema_reference")
            return inline(definitions[reference.removeprefix("#/$defs/")])
        result = {key: inline(item) for key, item in value.items() if key not in {"$schema", "$id", "$defs"}}
        if "const" in result:
            result["enum"] = [result.pop("const")]
        return result

    schema = inline(source)
    schema["description"] = (
        "按kind只填对应数据：relationship/flow用nodes、edges；swimlane再加lanes和每个节点的lane_id；"
        "sequence只用participants、messages；statistical只用chart_type、categories、series。"
        "每个对象都必须有英文开头的唯一id，系列还必须有name、values。"
        "先核对节点/连线引用与泳道，再核对分类数量和每组数值数量一致。"
        "无文字的连线省略label，禁止空字符串；数值用十进制字符串，缺失用不带引号的JSON null，禁止字符串\"null\"；"
        "legend、axis_labels只能为布尔值。不要从其他类型复制多余字段，不填写布局坐标。"
        "用户未指定样式时省略style、relation_type等可选装饰字段，采用默认样式，减少重复参数。"
    )
    return schema


def describe_chart_tools(tools):
    """Enrich only a chart tool already admitted; never mutate shared schemas."""
    if not any(isinstance(tool, dict) and tool.get("function", {}).get("name") == "chart_generate" for tool in tools or []):
        return tools
    result = deepcopy(tools)
    for tool in result:
        function = tool.get("function", {})
        if function.get("name") == "chart_generate":
            parameters = function.setdefault("parameters", {"type": "object"})
            parameters.setdefault("properties", {})["definition"] = deepcopy(_definition_schema())
            required = parameters.setdefault("required", [])
            if "definition" not in required:
                required.append("definition")
    return result
