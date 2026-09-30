"""Model-visible chart contract; execution is exclusively mediated by Runtime."""

from typing import Any


def normalize_chart_input(tool_name, tool_input):
    """Normalize empty edge labels and a quoted null without inventing content.

    All other invalid shapes and values remain for Runtime's strict validator.
    Copy changed containers so model input and audit source are never mutated.
    """
    if tool_name != "chart_generate" or not isinstance(tool_input, dict):
        return tool_input
    definition = tool_input.get("definition")
    if not isinstance(definition, dict):
        return tool_input
    if definition.get("kind") == "statistical":
        series = definition.get("series")
        if not isinstance(series, list) or not all(
            isinstance(item, dict) and isinstance(item.get("values"), list)
            for item in series
        ):
            return tool_input
        if not any(value == "null" for item in series for value in item["values"]):
            return tool_input
        normalized_series = [
            {**item, "values": [None if value == "null" else value for value in item["values"]]}
            for item in series
        ]
        return {**tool_input, "definition": {**definition, "series": normalized_series}}
    edges = definition.get("edges")
    if not isinstance(edges, list) or not all(isinstance(edge, dict) for edge in edges):
        return tool_input
    if not any(edge.get("label") == "" for edge in edges):
        return tool_input
    normalized = [
        {key: value for key, value in edge.items() if key != "label"}
        if edge.get("label") == "" else dict(edge)
        for edge in edges
    ]
    return {**tool_input, "definition": {**definition, "edges": normalized}}


async def chart_generate(
    definition: dict[str, Any], source_refs: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """Generate an editable chart using the bank-chart skill and chart/1 schema.

    Args:
        definition: Pure JSON with schema_version="chart/1", kind (relationship,
            flow, swimlane, sequence, statistical), title and type-specific data.
            Graphs use nodes(id,label,shape) and edges(id,source_id,target_id,
            optional non-empty label, optional ratio as decimal string 0..1).
            Omit label for unlabelled edges; do not send an empty string.
            Shapes: rectangle, rounded,
            ellipse, diamond, text. Swimlanes add lanes(id,label,order) and node
            lane_id. Sequence uses participants(id,label,order) and messages
            (id,from_id,to_id,order,label,message_type=request|return). Statistics
            use chart_type=column|bar|line|area|pie|donut, categories, series(id,name,
            values as decimal strings or unquoted JSON null, never "null").
            Optional unit is a string;
            legend and axis_labels are boolean switches (true/false), not objects
            or axis titles. Use missing_values="gap" to preserve null gaps.
            Do not supply coordinates, identity, HTML, scripts, paths or URLs.
        source_refs: Authorized sources as kind=file|tool_result|conversation,
            ref_id, complete, optional location/note/file_scope (session_file,
            workspace_file, generated_file). Reuse actual file IDs or
            tool-call IDs; do not invent references. A direct user description
            may use an empty list. Missing/incomplete inputs are not fabricated.
    """
    raise RuntimeError("图表工具必须通过 Bank Runtime Tool Gateway 执行。")


async def chart_export(chart_id: str, format: str, version_id: str = '') -> dict[str, Any]:
    """Export an existing editable chart's saved version without rebuilding it.

    Args:
        chart_id: Actual authorized chart identifier returned by Runtime.
        version_id: Omit or pass empty to export the current latest saved version.
            Runtime resolves and freezes its real identifier for this operation.
            Supply an actual version ID only when exporting that specific saved
            version. Never invent IDs or reconstruct a definition for export.
        format: Requested png, svg, pdf or vsdx (vsdx excludes statistical charts).
            Honor the user's explicit format. A render failure is not permission
            to replace PNG with SVG or to change numeric values and retry.
    """
    raise RuntimeError("图表导出必须通过 Bank Runtime Tool Gateway 执行。")


def chart_export_model_result(envelope):
    from .presentation import failure_message
    raw = envelope.get('result') or {}
    result = {key: raw[key] for key in ('chart_id', 'version_id', 'export_job_id', 'format',
              'artifact_status', 'generated_file_ids', 'mime_type') if key in raw}
    ready = (envelope.get('status') == 'success' and result.get('artifact_status') == 'succeeded'
             and result.get('generated_file_ids') and result.get('chart_id') and result.get('version_id'))
    if envelope.get('status') != 'success':
        result['retryable'] = False
        result['reason'] = str(envelope.get('error_code') or 'CHART_EXPORT_FAILED')
    message = ('图表已按指定格式导出，可通过文件卡片打开或下载。' if ready else
               failure_message(str(envelope.get('error_code') or '')) +
               ' 指定格式尚未交付；不要改用其他格式或修改数值类型重试。')
    return {'status': envelope.get('status'), 'result': result,
            'presentation': {'outcome': 'completed' if ready else 'failed' if envelope.get('status') != 'success' else 'unknown',
                             'message': message}}


def chart_model_result(envelope):
    raw = envelope.get("result") or {}
    keys = ("chart_id", "version_id", "kind", "title", "chart_status")
    result = {k: raw[k] for k in keys if k in raw}
    ready = (
        envelope.get("status") == "success"
        and result.get("chart_status") == "ready"
        and result.get("chart_id")
        and result.get("version_id")
    )
    return {
        "status": envelope.get("status"),
        "result": result,
        "presentation": {
            "outcome": (
                "completed"
                if ready
                else "failed" if envelope.get("status") != "success" else "unknown"
            ),
            "message": (
                "图表已生成，可打开卡片编辑、保存到工作区或下载。"
                if ready
                else "图表尚未确认生成完成，请勿重复提交结果未知的操作。"
            ),
        },
    }
