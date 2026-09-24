"""Model-visible chart contract; execution is exclusively mediated by Runtime."""

from typing import Any


async def chart_generate(
    definition: dict[str, Any], source_refs: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """Generate an editable chart using the bank-chart skill and chart/1 schema.

    Args:
        definition: Pure JSON with schema_version="chart/1", kind (relationship,
            flow, swimlane, sequence, statistical), title and type-specific data.
            Graphs use nodes(id,label,shape) and edges(id,source_id,target_id,label,
            optional ratio as decimal string 0..1). Shapes: rectangle, rounded,
            ellipse, diamond, text. Swimlanes add lanes(id,label,order) and node
            lane_id. Sequence uses participants(id,label,order) and messages
            (id,from_id,to_id,order,label,message_type=request|return). Statistics
            use chart_type=column|bar|line|area|pie|donut, categories, series(id,name,
            values as decimal strings or null), optional unit/legend/axis_labels.
            Do not supply coordinates, identity, HTML, scripts, paths or URLs.
        source_refs: Authorized sources as kind=file|tool_result|conversation,
            ref_id, complete, optional location/note/file_scope (session_file,
            workspace_file, generated_file). Reuse actual file IDs or
            tool-call IDs; do not invent references. A direct user description
            may use an empty list. Missing/incomplete inputs are not fabricated.
    """
    raise RuntimeError("图表工具必须通过 Bank Runtime Tool Gateway 执行。")


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
