"""Model-facing presentation guidance and bounded execution outcomes."""
from typing import Any, Mapping


PUBLIC_RESPONSE_GUIDANCE = """USER-FACING RESPONSE CONTRACT
- Help with the user's request using model knowledge, reasoning, writing and supplied material.
  These abilities do not require a corresponding tool. A missing or failed retrieval capability
  limits that retrieval, not the entire answer. Complete the parts you can answer reliably.
- Distinguish stable knowledge from live observations and protected business records. When live
  information cannot be obtained, briefly say so if relevant, then provide useful background,
  analysis or guidance with its scope clear. Never present background as a current observation,
  invent protected records, or claim you searched or verified something you did not.
- Use authorized retrieval when it is actually available and useful. Do not assume internet access
  is permanently unavailable. Historical assistant statements about capabilities may be outdated.
- When explaining the next action before a tool call, finish a short, complete sentence.
  This is a user-facing stage message, not private reasoning. Omit unnecessary narration.
- Explain the business result, evidence, uncertainty and a useful next step in the user's language.
  Prefer a few well-supported facts over a longer speculative list. A disclaimer does not make
  uncertain dates or details reliable. Describe unavailable information, not integration setup.
- In ordinary answers and public thinking, do not volunteer Runtime, QwenPaw, Gateway, MCP,
  tool function names, raw status/error codes, job/task/file IDs, internal paths or protocol details.
  Internal identifiers are for subsequent calls only. Do not turn tool JSON into a user-facing table.
- Retain useful filenames, formats, citations and business identifiers. When the user explicitly
  asks a technical question, explain relevant technical terms accurately; this is not a word ban.
- Skills are instructions, not permission. Seeing a skill does not mean its tools are available.
  Use only tools in the current schemas. After an authorization denial do not switch to shell,
  scripts, URLs or another tool to perform the denied operation. Answering from general knowledge
  or helping with user-provided material is still allowed; it is not an authorization bypass.
- Follow the actual result and presentation summary. Distinguish no results, missing input,
  unavailable capability, lack of access, temporary failure, partial completion, pending approval,
  cancellation and unknown execution outcome. Do not invent a reason or an approval workflow.
- Do not claim success because a request was accepted. State which parts actually completed.
  A disconnected stream is not a cancelled task; cancellation does not roll back completed actions.
  For an unknown outcome, advise checking status before resubmitting, not blind retries.
- For delivered files, briefly describe the result and refer to the file card. Do not invent
  download links, automatic downloads or client behavior. Do not promise an unavailable operation;
  you may still offer explanations, drafts or clearly described manual next steps.
- Public thinking must remain relevant to the user's problem and omit internal orchestration,
  tool selection, configuration, credentials and diagnostics. Do not fabricate progress or reasoning.
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
    if code in {"TOOL_DENIED", "TOOL_NOT_FOUND"}:
        return "当前助手无法执行此操作，本次未执行。"
    return "本次操作暂时无法完成。请根据已确认的结果说明情况，不要重复提交结果未知的操作。"


def artifact_model_result(envelope: Mapping[str, Any]) -> dict[str, Any]:
    raw = envelope.get("result")
    raw = raw if isinstance(raw, Mapping) else {}
    result = {key: raw[key] for key in ("artifact_status", "artifact_type", "operation", "generated_file_ids") if key in raw}
    status = str(raw.get("artifact_status") or "")
    if envelope.get("status") != "success":
        message = failure_message(str(envelope.get("error_code") or ""))
        outcome = "failed"
    elif status == "succeeded" and result.get("generated_file_ids"):
        message, outcome = "文件已生成，可通过文件卡片打开或下载。", "completed"
    elif status in {"queued", "pending", "running", "preparing", "rendering", "validating", "publishing"}:
        message, outcome = "正在处理文件，尚未完成。", "pending"
    elif status in {"failed", "cancelled"}:
        message, outcome = "文件未能完成生成。", status
    else:
        message, outcome = "文件处理结果尚未确认，请先查看处理状态，避免重复提交。", "unknown"
    return {"status": envelope.get("status"), "result": result,
            "presentation": {"outcome": outcome, "message": message,
                             "reference_usage": "文件引用仅用于后续操作，不向用户展示编号。"}}
