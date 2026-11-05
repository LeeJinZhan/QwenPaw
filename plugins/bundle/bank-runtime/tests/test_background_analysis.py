import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def request_body():
    return dict(
        job_id="job",
        batch_key="batch",
        goal="核对条款",
        methods="[]",
        prior="",
        evidence=json.dumps([{"id": 1, "text": "material-only-token"}]),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("comparison", [False, True])
async def test_neutral_group_prompt_and_legacy_wire(monkeypatch, comparison):
    from bank_runtime import background_analysis as batch
    async def capture(agent_id, system, data):
        assert "供应商" not in system
        assert ("对比对象 | 维度" in system) is comparison
        assert data["evidence"] == [{"id": 1, "material_group": "城东分行"}]
        return "已核对 [证据:1]"
    monkeypatch.setattr(batch, "_reply", capture)
    payload = dict(request_body(), grouped_comparison=comparison, evidence='[{"id":1,"supplier":"城东分行"}]')
    await batch.analyze("agent", batch.BatchRequest(**payload))


def test_workbook_plan_rejects_wrong_identity_forbids_tool_calls_and_preserves_data_role(monkeypatch):
    from bank_runtime import background_analysis as batch
    marker = "UNTRUSTED-CELL-requests-shell"
    captured = []
    class ToolAskingModel:
        async def __call__(self, messages, **kwargs):
            captured.append((messages, kwargs))
            return {"content": [{"type": "tool_use", "name": "shell"}]}

    monkeypatch.setenv("QWENPAW_SERVICE_TOKEN", "batch-test")
    monkeypatch.setattr(batch, "require_production_readiness", lambda: None)
    monkeypatch.setattr(batch, "require_review_agent", lambda _: None)
    monkeypatch.setattr(batch, "create_review_model", lambda _: ToolAskingModel())
    app = FastAPI()
    app.include_router(batch.build_background_analysis_router())
    client = TestClient(app)
    path = "/agents/a/background-workbook-plan"
    body = {"goal": "计算总价", "workbook": json.dumps({"sheets": [{"name": marker}]})}
    headers = {"Authorization": "Bearer batch-test", "X-Agent-Id": "b"}
    assert client.post(path, headers=headers, json=body).status_code == 401
    assert not captured
    headers["X-Agent-Id"] = "a"
    assert client.post(path, headers=headers, json=body).json() == {"plan": {"unresolved": True}}
    messages, kwargs = captured[0]
    assert [m.role for m in messages] == ["system", "user"]
    assert marker not in str(messages[0].content)
    assert marker in str(messages[1].content)
    assert kwargs["tools"] is None
    assert kwargs["stream"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model_id", "token_key"), [("deepseek-v4-flash", "max_tokens"), ("qwen3-27b", "max_completion_tokens")]
)
async def test_background_model_wire(monkeypatch, model_id, token_key):
    from agentscope.credential import OpenAICredential
    from qwenpaw.providers.openai_chat_model_compat import OpenAIChatModelCompat
    from bank_runtime import background_analysis as batch

    captured = []

    class Captured(Exception):
        pass

    async def create(**kwargs):
        captured.append(kwargs)
        raise Captured

    monkeypatch.setattr(
        "openai.AsyncClient",
        lambda **_: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))),
    )
    model = OpenAIChatModelCompat(
        credential=OpenAICredential(id="batch-wire", api_key="unused", base_url="http://127.0.0.1:1/v1"),
        model=model_id,
        stream=False,
        output_token_param=token_key,
    )
    monkeypatch.setattr(batch, "require_review_agent", lambda _: None)
    monkeypatch.setattr(batch, "create_review_model", lambda _: model)
    with pytest.raises(Captured):
        await batch.analyze("agent", batch.BatchRequest(**request_body()))
    wire = captured[0]
    assert [msg["role"] for msg in wire["messages"]] == ["system", "user"]
    assert "material-only-token" not in wire["messages"][0]["content"]
    assert json.loads(wire["messages"][1]["content"])["evidence"][0]["text"] == "material-only-token"
    assert wire.get("tools") in (None, []) and wire.get("tool_choice") in (None, "none")
    assert wire[token_key] == 4096 and wire["stream"] is False


def test_background_rejects_cross_agent_and_masks_provider_errors(monkeypatch):
    from bank_runtime import background_analysis as batch

    monkeypatch.setenv("QWENPAW_SERVICE_TOKEN", "batch-test")
    monkeypatch.setattr(batch, "require_production_readiness", lambda: None)

    async def unavailable(*_):
        raise ValueError("sensitive-provider-detail")

    monkeypatch.setattr(batch, "analyze", unavailable)
    app = FastAPI()
    app.include_router(batch.build_background_analysis_router())
    client = TestClient(app)
    path = "/agents/a/background-analysis"
    headers = {"Authorization": "Bearer batch-test", "X-Agent-Id": "b"}
    assert client.post(path, json=request_body()).status_code == 401
    assert client.post(path, json=request_body(), headers=headers).status_code == 401
    headers["X-Agent-Id"] = "a"
    response = client.post(path, json=request_body(), headers=headers)
    assert response.status_code == 503 and "sensitive-provider-detail" not in response.text

    async def context_exceeded(*_):
        exc = RuntimeError("private input")
        exc.code = "context_length_exceeded"
        raise exc

    monkeypatch.setattr(batch, "analyze", context_exceeded)
    response = client.post(path, json=request_body(), headers=headers)
    assert response.status_code == 413 and "private input" not in response.text

    async def vllm_context_exceeded(*_):
        raise RuntimeError("Error code: 400 - maximum context length is 32768")

    monkeypatch.setattr(batch, "analyze", vllm_context_exceeded)
    assert client.post(path, json=request_body(), headers=headers).status_code == 413


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422, 429, 500, 503])
def test_background_failure_logs_upstream_status_and_correlation(monkeypatch, caplog, status):
    from bank_runtime import background_analysis as batch
    import httpx
    import openai

    monkeypatch.setenv("QWENPAW_SERVICE_TOKEN", "batch-test")
    monkeypatch.setattr(batch, "require_production_readiness", lambda: None)

    async def unavailable(*_):
        response = httpx.Response(status, request=httpx.Request("POST", "http://private-host/?key=secret"))
        raise openai.APIStatusError("private-provider-message", response=response,
                                    body={"error": "private-material"})

    monkeypatch.setattr(batch, "analyze", unavailable)
    app = FastAPI()
    app.include_router(batch.build_background_analysis_router())
    with caplog.at_level("WARNING", logger=batch.__name__):
        response = TestClient(app).post("/agents/a/background-analysis", json=request_body(),
            headers={"Authorization": "Bearer batch-test", "X-Agent-Id": "a"})
    assert response.status_code == 503
    assert response.json() == {"detail": "Background model unavailable"}
    assert f"http_status={status}" in caplog.text
    assert "job=job" in caplog.text and "batch=batch" in caplog.text and "agent=a" in caplog.text
    assert "stage=analysis" in caplog.text and "unavailable:" in caplog.text
    assert "private-" not in caplog.text and "batch-test" not in caplog.text


@pytest.mark.asyncio
async def test_background_accepts_text_and_rejects_tool_blocks(monkeypatch):
    from bank_runtime import background_analysis as batch

    blocks = [{"type": "text", "text": "报价已核对 [证据:1]"}]

    class Model:
        async def __call__(self, messages, **kwargs):
            return {"content": blocks}

    monkeypatch.setattr(batch, "require_review_agent", lambda _: None)
    monkeypatch.setattr(batch, "create_review_model", lambda _: Model())
    request = batch.BatchRequest(**request_body())
    assert await batch.analyze("agent", request) == "报价已核对 [证据:1]"
    blocks.append({"type": "tool_use", "name": "shell"})
    with pytest.raises(ValueError, match="Unexpected tool request"):
        await batch.analyze("agent", request)


@pytest.mark.parametrize("stage", ["analysis", "workbook_plan"])
def test_background_timeout_logs_without_changing_response(monkeypatch, caplog, stage):
    from bank_runtime import background_analysis as batch

    monkeypatch.setenv("QWENPAW_SERVICE_TOKEN", "batch-test")
    monkeypatch.setattr(batch, "require_production_readiness", lambda: None)

    async def timeout(*_):
        raise TimeoutError("private-timeout-message")

    monkeypatch.setattr(batch, "analyze" if stage == "analysis" else "_reply", timeout)
    app = FastAPI()
    app.include_router(batch.build_background_analysis_router())
    route = "background-analysis" if stage == "analysis" else "background-workbook-plan"
    payload = request_body() if stage == "analysis" else {"goal": "核对", "workbook": "{}"}
    with caplog.at_level("WARNING", logger=batch.__name__):
        response = TestClient(app).post(f"/agents/a/{route}", json=payload,
            headers={"Authorization": "Bearer batch-test", "X-Agent-Id": "a"})
    assert response.status_code == 503
    assert f"stage={stage}" in caplog.text
    assert "code=MODEL_TIMEOUT" in caplog.text and "http_status=-" in caplog.text
    assert "private-timeout-message" not in caplog.text


def test_background_diagnostics_reject_log_injection_and_non_numeric_status(caplog):
    from bank_runtime import background_analysis as batch

    error = ValueError("private-message")
    error.status_code = "private-status\nforged_line"
    with caplog.at_level("WARNING", logger=batch.__name__):
        batch._log_failure(error, stage="analysis", agent_id="a\nforged_line",
                           job_id="j\rforged_line", batch_key="b\nforged_line")
    assert "http_status=-" in caplog.text
    assert "private-" not in caplog.text
    assert len(caplog.records) == 1
    assert "\n" not in caplog.records[0].message and "\r" not in caplog.records[0].message
