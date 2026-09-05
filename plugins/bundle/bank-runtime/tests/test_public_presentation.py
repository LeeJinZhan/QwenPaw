import json
from pathlib import Path
import sys
from types import SimpleNamespace
import pytest
from agentscope.permission import PermissionBehavior

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bank_runtime.gateway.middleware import BankRuntimeGatewayMiddleware, GatewayPermissionEngine, _runtime_tool_response
from bank_runtime.gateway.client import GatewayError


@pytest.mark.asyncio
async def test_gateway_diagnostic_is_not_model_facing():
    class Client:
        async def preflight(self, *args, **kwargs):
            raise GatewayError("Runtime Tool Gateway denied secret detail", code="POLICY_BLOCKED")
    engine = GatewayPermissionEngine(None, BankRuntimeGatewayMiddleware(Client()))
    result = await engine.check_permission(SimpleNamespace(name="browser"), {})
    assert result.behavior == PermissionBehavior.DENY
    assert "Runtime" not in result.message
    assert "不允许" in result.message


@pytest.mark.parametrize("status,fragment", [("succeeded", "已生成"), ("failed", "未能"), ("running", "正在"), ("unknown", "尚未确认")])
def test_artifact_presentation_uses_actual_job_status_and_preserves_only_needed_refs(status, fragment):
    response = _runtime_tool_response("call1", {"status": "success", "tool_call_id": "internal",
        "result": {"artifact_status": status, "artifact_job_id": "internal_job",
                   "generated_file_ids": ["gfile_1"], "artifact_type": "markdown"}})
    payload = json.loads(response.content[0].text)
    assert fragment in payload["presentation"]["message"]
    assert "internal_job" not in response.content[0].text
    assert payload["result"]["generated_file_ids"] == ["gfile_1"]
