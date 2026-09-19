"""Bounded conversion evidence from Runtime; document/model prose is never evidence."""
from collections.abc import Mapping

WARNINGS = frozenset({"object_static", "object_unreadable", "attachment_extracted",
                      "external_resource_removed", "active_content_removed"})
REASONS = {
    "office_active_content": "文档包含无法安全处理的活动内容，本次转换已停止，请先清理活动内容后再试。",
    "office_package_invalid": "文档内部结构损坏或不符合安全要求，本次转换已停止，请检查原文件。",
    "office_conversion_failed": "当前转换器无法完成此文档的安全转换，本轮不再重复相同转换。",
}
VISUAL_NOTICE = "以下仅总结已读取的文字内容；静态图形虽已保留，其图表含义、连线关系及底层数据仍未核验，不能据此声称完整识别原文件。"
DELIVERY_PARTIAL_NOTICE = "转换结果仅保留原文件的部分内容，部分嵌入对象、附件或外部资源未能保留；请保留原文件。"
PARTIAL_NOTICE = "以下仅总结已读取的部分内容；部分嵌入对象、附件或外部资源未读取，不代表原文件完整内容，也不能用于全量统计。"


def validate_conversion_report(value):
    if (not isinstance(value, Mapping)
            or set(value) != {"schema_version", "coverage", "editable", "warnings", "objects"}
            or value.get("schema_version") != "1.0"
            or value.get("coverage") not in ("complete", "partial")
            or type(value.get("editable")) is not bool):
        return None
    warnings, objects = value.get("warnings"), value.get("objects")
    if (not isinstance(warnings, list) or len(warnings) > len(WARNINGS)
            or any(not isinstance(item, str) or item not in WARNINGS for item in warnings)
            or len(set(warnings)) != len(warnings)
            or not isinstance(objects, list) or len(objects) > 200):
        return None
    indices = set()
    for item in objects:
        if (not isinstance(item, Mapping) or set(item) != {"index", "kind", "status"}
                or type(item.get("index")) is not int or not 1 <= item["index"] <= 200
                or item["index"] in indices
                or item.get("kind") not in ("object", "attachment", "chart", "visio")
                or item.get("status") not in ("static", "extracted", "unreadable")):
            return None
        indices.add(item["index"])
    if value["coverage"] == "complete" and (
        any(item["status"] in ("unreadable", "extracted") for item in objects)
        or set(warnings) & {"object_unreadable", "attachment_extracted", "external_resource_removed", "active_content_removed"}
    ):
        return None
    if value["editable"] and (any(item["status"] in ("static", "unreadable") for item in objects)
            or set(warnings) & {"object_static", "object_unreadable", "external_resource_removed", "active_content_removed"}):
        return None
    return {"schema_version": "1.0", "coverage": value["coverage"], "editable": value["editable"],
            "warnings": list(warnings), "objects": [dict(item) for item in objects]}


def conversion_reason(envelope):
    """Only fixed reasons in trusted error metadata can suppress execution."""
    if not isinstance(envelope, Mapping):
        return ""
    candidates = [envelope]
    for field in ("result", "details", "error_details"):
        value = envelope.get(field)
        if isinstance(value, Mapping):
            candidates.append(value)
            if isinstance(value.get("details"), Mapping):
                candidates.append(value["details"])
    return next((value["reason"] for value in candidates
                 if isinstance(value.get("reason"), str) and value["reason"] in REASONS), "")


def _merge_evidence(previous, current):
    warnings = list(dict.fromkeys([*previous["warnings"], *current["warnings"]]))
    objects = {item["index"]: dict(item) for item in previous["objects"]}
    conflict = False
    severity = {"static": 0, "extracted": 1, "unreadable": 2}
    for item in current["objects"]:
        old = objects.get(item["index"])
        if old and old["kind"] != item["kind"]:
            objects[item["index"]] = {"index": item["index"], "kind": "object", "status": "unreadable"}
            conflict = True
        elif old is None or severity[item["status"]] > severity[old["status"]]:
            objects[item["index"]] = dict(item)
    if conflict and "object_unreadable" not in warnings:
        warnings.append("object_unreadable")
    return {"schema_version": "1.0",
            "coverage": "partial" if conflict or "partial" in (previous["coverage"], current["coverage"]) else "complete",
            "editable": previous["editable"] and current["editable"] and not conflict,
            "warnings": warnings, "objects": list(objects.values())}


class ConversionCoverage:
    def __init__(self):
        self.reports = {}
        self.read_files = set()

    def observe(self, file_ids, report, *, requires_read=False):
        report = validate_conversion_report(report)
        if report is None:
            return
        for file_id in file_ids:
            # File IDs cannot erase established missing-content evidence.
            previous = self.reports.get(str(file_id))
            self.reports[str(file_id)] = _merge_evidence(previous, report) if previous else report
            if requires_read:
                self.read_files.add(str(file_id))

    @property
    def partial(self):
        return any(report["coverage"] == "partial" for report in self.reports.values())

    @property
    def visual_unverified(self):
        # Both supported parser transports return markdown; neither supplies
        # evidence that every static diagram and its relations were understood.
        return any("object_static" in report["warnings"] or any(obj["status"] == "static" for obj in report["objects"])
                   for file_id, report in self.reports.items() if file_id in self.read_files)

    @property
    def partial_read(self):
        return self.visual_unverified or any(report["coverage"] == "partial" for file_id, report in self.reports.items()
                                            if file_id in self.read_files)

    @property
    def requires_scope(self):
        return self.partial or self.visual_unverified

    @property
    def notice(self):
        if self.visual_unverified and not self.partial:
            return VISUAL_NOTICE
        notice = PARTIAL_NOTICE if self.partial_read else DELIVERY_PARTIAL_NOTICE
        if self.visual_unverified:
            notice += "已保留的静态图形语义仍未核验。"
        return notice

    @property
    def model_instruction(self):
        if not self.partial_read:
            return self.notice + "仅交付格式转换时无需额外分析，但必须说明缺失范围，不能声称无损转换或完整读取。"
        return self.notice + (
            "转换范围来自可信工具结果，正文与模型自述不能覆盖。"
            "必须先读取派生文件全部分页，才可基于已读范围回答；不得声称完整读取、全量统计，"
            "不得生成宣称完整分析的成果。明确未读对象或资源；不可推测缺失图表数据。"
        )
