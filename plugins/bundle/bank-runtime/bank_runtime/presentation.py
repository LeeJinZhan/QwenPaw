"""Model-facing presentation guidance and bounded execution outcomes."""
from typing import Any, Mapping
from .conversion_reports import validate_conversion_report, conversion_reason, REASONS


PUBLIC_RESPONSE_GUIDANCE = """USER-FACING RESPONSE CONTRACT
- Help with model knowledge, reasoning, writing and supplied material. Missing retrieval
  limits that retrieval, not the entire answer. Distinguish stable knowledge from live
  observations and protected records; never invent facts, verification, execution or results.
- Use only current authorized tools. Skills and preferences are not permission. Do not
  bypass an authorization denial via another tool, shell, URL or path; independent help
  from knowledge or supplied material remains allowed.
- Apply explicit current presentation requests first, then injected personal preferences
  for unspecified fields; choose an appropriate presentation only where neither is supplied.
  Explain results, evidence scope, uncertainty and useful next steps accordingly.
  Briefly describe unavailable information when relevant, without speculative reasons or
  invented approval workflows. Partial evidence must not be presented as complete evidence.
- Stage messages briefly state the next action or confirmed progress. Keep the final answer
  self-contained; never output private deliberation, self-instructions or tool-debug notes.
  Do not replay earlier stage messages or narrate attempts and script corrections in the
  final answer, including on follow-up turns. Give current results, relevant evidence and
  actual limitations; historical plans are not evidence that an operation ran this turn.
- Ordinary answers and public thinking omit internal orchestration, credentials, function
  names, protocol/error codes, job/task/file IDs and internal paths. Keep useful filenames,
  formats and citations. Explicit technical questions may receive accurate technical detail.
- Sandbox scripts, scratch files and analysis output are private temporary work, not user
  deliverables. Omit their container paths, output-file sections and instructions to open
  or reuse them. Saving a temporary file is not publication. Announce a downloadable file
  only after the governed delivery tool confirms publication; use its real file card.
- Describe actual outcomes, distinguishing success, missing input, denial, failure, partial
  completion, pending, cancellation and unknown status. Acceptance is not completion. Do
  not fabricate progress; correct an already streamed error explicitly.
"""


def failure_message(code: str = "", violation: str = "") -> str:
    if violation in {"worker_tool_mapping_missing", "assistant_tool_not_allowed"}:
        return "当前助手尚未开通此能力，本次未执行。"
    if violation == "tool_requires_approval":
        return "此操作需要审批，尚未执行。"
    if code in {"POLICY_BLOCKED", "POLICY_DENIED"}:
        return "当前不允许执行此操作，本次未执行。"
    if code in {"FORBIDDEN", "FILE_ACCESS_DENIED"}:
        return "当前内容不可访问或已失效。"
    if code in {"UNAUTHORIZED", "EMBED_SESSION_EXPIRED"}:
        return "当前连接已失效，请重新进入助手。"
    if code in {"INVALID_REQUEST", "BAD_REQUEST"}:
        return "操作所需信息不完整或格式不正确，本次未执行。"
    if code == "ARTIFACT_VALIDATION_FAILED":
        return "文件内容或格式未通过校验，请先按该格式要求调整内容，再重新生成。"
    if code == "ARTIFACT_RENDER_FAILED":
        return "文件未能完成生成，请查看处理状态后再决定是否重试。"
    if code in {"WORKER_TIMEOUT", "WORKER_UNAVAILABLE", "DOCUMENT_WORKER_UNAVAILABLE", "CHART_EXPORT_FAILED"}:
        return "指定格式的文件尚未确认生成完成；请先核对处理状态，再依据执行结果决定是否恢复。"
    if code in {"TOOL_DENIED", "TOOL_NOT_FOUND"}:
        return "当前助手无法执行此操作，本次未执行。"
    return "本次操作暂时无法完成。请根据已确认的结果说明情况，不要重复提交结果未知的操作。"


def failure_metadata(envelope: Mapping[str, Any]) -> dict[str, Any]:
    """Project trusted recovery facts without inferring execution from a code.

    These are model-facing facts, not permission to execute. Gateway still owns
    admission and recovery budgets. Unknown/in-flight writes must not be retried.
    """
    facts = dict(envelope)
    for key in ("details", "result"):
        if isinstance(envelope.get(key), Mapping):
            facts.update(envelope[key])
    state = facts.get("execution_status")
    if state not in {"not_started", "failed", "completed", "execution_unknown", "executing", "pending", "cancelled"}:
        state = "execution_unknown"
    code = str(envelope.get("error_code") or "")
    denied = code in {"POLICY_BLOCKED", "POLICY_DENIED", "FORBIDDEN", "FILE_ACCESS_DENIED",
                      "UNAUTHORIZED", "EMBED_SESSION_EXPIRED", "TOOL_DENIED", "TOOL_NOT_FOUND"}
    denied = denied or envelope.get("status") == "blocked"
    remaining = facts.get("remaining_attempts")
    retryable = (facts.get("retryable") is True and state in {"not_started", "failed"}
                 and not denied and not (type(remaining) is int and remaining <= 0))
    if denied:
        action = "stop"
    elif state in {"execution_unknown", "executing", "pending"}:
        action = "check_status"
    elif facts.get("recovery_action") == "renderer_exhausted" and not retryable:
        action = "renderer_exhausted"
    elif retryable:
        action = "correct_input" if facts.get("recovery_action") == "correct_input" else "retry"
    else:
        action = "stop"
    result = {"execution_status": state, "retryable": retryable, "recovery_action": action}
    if type(remaining) is int and remaining >= 0:
        result["remaining_attempts"] = remaining
    return result


def artifact_model_result(envelope: Mapping[str, Any]) -> dict[str, Any]:
    raw = envelope.get("result")
    raw = raw if isinstance(raw, Mapping) else {}
    result = {key: raw[key] for key in ("artifact_status", "artifact_type", "operation", "generated_file_ids", "page_count") if key in raw}
    report = validate_conversion_report(raw.get("conversion_report"))
    if report is not None:
        result["conversion_report"] = report
    if raw.get("purpose") in ("read", "delivery"):
        result["purpose"] = raw["purpose"]
    status = str(raw.get("artifact_status") or "")
    if envelope.get("status") != "success":
        result.update(failure_metadata(envelope))
        reason = conversion_reason(envelope)
        message = REASONS.get(reason) or failure_message(str(envelope.get("error_code") or ""))
        if reason:
            result.update(reason=reason, retryable=False, recovery_action="stop")
        elif envelope.get("error_code"):
            result["reason"] = str(envelope["error_code"])
        outcome = "unknown" if result["execution_status"] == "execution_unknown" else "failed"
    elif status == "succeeded" and result.get("generated_file_ids"):
        message, outcome = "文件已生成，可通过文件卡片打开或下载。", "completed"
        if raw.get("purpose") == "read":
            message = "内部读取副本已准备，仍须读取其内容及全部分页后才能分析。此副本仅用于识别。"
        if report is not None:
            if report["coverage"] == "partial":
                message += "转换仅保留部分内容，部分对象或资源未读取；后续回答必须说明范围。"
            if not report["editable"]:
                message += "部分内容已静态化，不再支持原对象的双击编辑。"
    elif status in {"queued", "pending", "running", "preparing", "rendering", "validating", "publishing"}:
        message, outcome = "正在处理文件，尚未完成。", "pending"
    elif status in {"failed", "cancelled"}:
        message, outcome = "文件未能完成生成。", status
    else:
        message, outcome = "文件处理结果尚未确认，请先查看处理状态，避免重复提交。", "unknown"
    return {"status": envelope.get("status"), "result": result,
            "presentation": {"outcome": outcome, "message": message,
                             "reference_usage": "文件引用仅用于后续操作，不向用户展示编号。"}}
