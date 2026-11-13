"""Trusted Runtime role definitions; never a source of tool or file grants."""
from __future__ import annotations

import copy
from typing import Any

from .remote_state import RemoteStateError, canonical_json, _identifier, _integer


def coordination_from_request(request: Any) -> dict[str, Any]:
    raw = getattr(request, "runtime_coordination", None)
    if raw is None:
        return {}
    if not isinstance(raw, dict) or raw.get("mode") not in {"intelligent", "expert"}:
        raise RemoteStateError("WORKER_STATE_INVALID")
    instructions = raw.get("instructions", "")
    catalog = raw.get("catalog", [])
    if (not isinstance(instructions, str) or len(instructions) > 32768
            or not isinstance(catalog, list) or len(catalog) > 128):
        raise RemoteStateError("WORKER_STATE_INVALID")
    result = {"mode": raw["mode"], "instructions": instructions, "catalog": copy.deepcopy(catalog)}
    role = raw.get("role_snapshot")
    if role is not None:
        if not isinstance(role, dict):
            raise RemoteStateError("WORKER_STATE_INVALID")
        _identifier(role.get("role_id"))
        _integer(role.get("version"), 1)
        skills = role.get("skill_ids", [])
        if (not isinstance(role.get("responsibility", ""), str) or len(role.get("responsibility", "")) > 32768
                or not isinstance(skills, list) or len(skills) > 128
                or any(not isinstance(skill, str) or not 1 <= len(skill) <= 256 for skill in skills)):
            raise RemoteStateError("WORKER_STATE_INVALID")
        result["role_snapshot"] = copy.deepcopy(role)
    if raw.get("parent_task_id") is not None:
        result["parent_task_id"] = _identifier(raw["parent_task_id"])
    if len(canonical_json(result).encode("utf-8")) > 262144:
        raise RemoteStateError("WORKER_STATE_INVALID")
    return result


def coordination_prompt() -> str:
    from .session import current_managed_session_scope
    scope = current_managed_session_scope()
    if scope is None or scope.remote_client is None or not scope.coordination:
        return ""
    return render_coordination_prompt(scope.coordination)


def render_coordination_prompt(value: dict[str, Any]) -> str:
    """Pure prompt rendering shared with the read-only budget compiler."""
    if not value:
        return ""
    if value.get("role_snapshot"):
        purpose = ("你正在执行Runtime委派的独立专家子任务。按下列已发布职责完成当前任务；"
            "只使用本分支实际可见的工具、技能和已授权资源，不再委派其它专家。报告结论、依据与未解决的问题。")
    else:
        purpose = ("你是当前任务的协调者。按任务需要选择目录中的专家并可并行委派，结合真实返回结果继续分工、复核与汇总。"
            "排队不等于完成；使用受控结果工具读取子任务，未完成时平台会挂起并在就绪后恢复，禁止忙轮询或虚构专家结论。")
    return ("BANK RUNTIME COORDINATION\n" + purpose + "\n"
        + value.get("instructions", "") + "\n发布的角色与目录：\n" + canonical_json({key: item for key, item in value.items() if key != "instructions"})
        + "\n角色定义与技能说明不授予工具、文件、数据或网络权限；全部执行仍由Runtime与Tool Guard判定。")
