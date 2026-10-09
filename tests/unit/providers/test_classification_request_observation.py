"""Client
request evidence is observable without claiming unknown server defaults.
"""
from unittest.mock import AsyncMock

import pytest
from agentscope.credential import OpenAICredential
from agentscope.model import OpenAIChatModel

from qwenpaw.providers.classification_request_observation import (
    observe_openai_chat_request,
)
from qwenpaw.providers.openai_chat_model_compat import OpenAIChatModelCompat
from qwenpaw.providers.retry_chat_model import RetryChatModel
from qwenpaw.token_usage.model_wrapper import TokenRecordingModelWrapper


def native(**kwargs):
    return OpenAIChatModelCompat(
        credential=OpenAICredential(
            id="qwenpaw-approved",
            api_key="synthetic-secret",
            base_url="https://mock-provider.invalid/v1",
        ),
        provider_id="approved",
        model="actual-model",
        parameters=OpenAIChatModel.Parameters(**kwargs),
        stream=True,
    )


def test_observation_reports_omitted_fields_and_unknown_server_revisions():
    model = native()
    observed = model.get_classification_request_observation()
    assert observed["wire_parameters"] == {}
    assert observed["model_revision"] is observed["gateway_revision"] is None
    assert (
        observed["provider_id"] == "approved"
        and observed["model_id"] == "actual-model"
    )
    assert "synthetic-secret" not in str(observed)
    assert "mock-provider.invalid" not in str(observed)


def test_observation_merges_sdk_parameters_kwargs_and_transport_extra_body():
    model = native(temperature=0.2, top_p=0.8, max_tokens=400)
    observed = observe_openai_chat_request(
        model,
        model.model,
        {
            "temperature": 0.3,
            "seed": 7,
            "extra_body": {"temperature": 0.4, "max_completion_tokens": 500},
        },
    )
    assert observed["wire_parameters"] == dict(
        temperature=0.4, top_p=0.8, seed=7, max_completion_tokens=500
    )


def test_readonly_getter_uses_provider_native_token_conversion():
    model = native()
    model._extra_generate_kwargs = {
        "max_tokens": 500,
        "disable_thinking": True,
    }
    model._output_token_param = "max_completion_tokens"
    before = dict(model._extra_generate_kwargs)
    observed = model.get_classification_request_observation()
    assert observed["wire_parameters"]["max_completion_tokens"] == 500
    assert observed["wire_parameters"]["enable_thinking"] is False
    assert model._extra_generate_kwargs == before


@pytest.mark.parametrize(
    "override",
    [
        {"model": "other"},
        {"messages": []},
        {"tools": []},
        {"extra_body": {"model": "other"}},
        {"temperature": float("nan")},
        {"seed": object()},
    ],
)
def test_structural_or_unbounded_overrides_rejected(override):
    model = native()
    with pytest.raises((ValueError, TypeError)):
        observe_openai_chat_request(model, model.model, override)


def test_actual_model_name_and_endpoint_overrides_are_observed():
    model = native()
    original = model.get_classification_request_observation()[
        "endpoint_sha256"
    ]
    model.client_kwargs = {"base_url": "https://changed-provider.invalid/v1"}
    assert (
        model.get_classification_request_observation()["endpoint_sha256"]
        != original
    )
    with pytest.raises(ValueError):
        observe_openai_chat_request(model, "other-model", {})


def test_wrappers_forward_actual_instance_without_model_calls():
    model = native(temperature=0.1)
    request = AsyncMock()
    model._call_api = request
    wrapped = RetryChatModel(TokenRecordingModelWrapper("approved", model))
    before = wrapped.get_classification_request_observation()
    model.parameters.temperature = 0.7
    after = wrapped.get_classification_request_observation()
    assert before["wire_parameters"]["temperature"] == 0.1
    assert after["wire_parameters"]["temperature"] == 0.7
    request.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "override",
    [
        {"temperature": 0.9},
        {"extra_body": {"temperature": 0.9}},
        {"model": "other"},
        {"extra_body": {"model": "other"}},
    ],
)
async def test_final_wire_guard_rejects_drift_before_sdk_request(
    monkeypatch, override
):
    from qwenpaw.providers.classification_request_observation import (
        observe_openai_chat_request,
    )
    from hashlib import sha256
    import json

    model = native(temperature=0.1)

    def digest(value):
        return sha256(
            json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
        ).hexdigest()

    expected = digest(model.get_classification_request_observation())
    stopped = AsyncMock()
    sdk = AsyncMock(return_value="synthetic-response")
    monkeypatch.setattr(OpenAIChatModel, "_call_api", sdk)

    async def guard(value):
        if value is None or digest(value) != expected:
            await stopped()
            raise ValueError("CLASSIFICATION_WIRE_DRIFT")

    model.bind_classification_request_guard(guard)
    with pytest.raises(ValueError, match="WIRE_DRIFT"):
        await model._call_api(model.model, [], **override)
    sdk.assert_not_called()
    stopped.assert_awaited_once()


@pytest.mark.asyncio
async def test_wire_guard_accepts_same_observation_other_models_unchanged(
    monkeypatch,
):
    model = native(temperature=0.1)
    seen = []
    sdk = AsyncMock(return_value="synthetic-response")
    monkeypatch.setattr(OpenAIChatModel, "_call_api", sdk)

    async def guard(value):
        seen.append(value)

    model.bind_classification_request_guard(guard)
    assert await model._call_api(model.model, []) == "synthetic-response"
    assert seen == [model.get_classification_request_observation()]
    unbound = native()
    assert (
        await unbound._call_api(
            unbound.model, [], arbitrary_provider_option=True
        )
        == "synthetic-response"
    )
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_native_sdk_transport_json_matches_observation(monkeypatch):
    import httpx
    import json
    import openai
    from agentscope.message import Msg

    original = openai.AsyncClient
    captured = []

    async def transport(request):
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "synthetic",
                "object": "chat.completion",
                "created": 1,
                "model": "actual-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    monkeypatch.setattr(
        openai,
        "AsyncClient",
        lambda **kwargs: original(**kwargs, http_client=client),
    )
    model = native(
        temperature=0.2, top_p=0.8, max_tokens=400, parallel_tool_calls=False
    )
    model.stream = False
    model._extra_generate_kwargs = {
        "extra_body": {"temperature": 0.4, "seed": 7}
    }
    observed = model.get_classification_request_observation()
    guarded = []
    model.bind_classification_request_guard(
        lambda value: guarded.append(value)
    )
    tools = [
        {
            "type": "function",
            "function": {
                "name": "claim_tasks",
                "description": "claim",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    await model._call_api(
        model.model,
        [
            Msg(
                name="user",
                content=[{"type": "text", "text": "synthetic task"}],
                role="user",
            )
        ],
        tools=tools,
    )
    assert len(captured) == 1
    body = captured[0]
    assert (
        body["model"] == observed["model_id"]
        and body["stream"] is observed["stream"]
    )
    assert {
        key: value
        for key, value in body.items()
        if key not in {"model", "messages", "stream", "tools"}
    } == observed["wire_parameters"]
    assert guarded == [observed]
    await client.aclose()


@pytest.mark.asyncio
async def test_final_header_normalization_failure_stops_classification(
    monkeypatch,
):
    model = native()
    model._default_headers = {"x-application": "classification"}
    stopped = AsyncMock()
    sdk = AsyncMock()
    monkeypatch.setattr(OpenAIChatModel, "_call_api", sdk)

    async def guard(value):
        assert value is None
        await stopped()
        raise ValueError("CLASSIFICATION_WIRE_DRIFT")

    model.bind_classification_request_guard(guard)
    with pytest.raises(ValueError, match="WIRE_DRIFT"):
        await model._call_api(model.model, [], extra_headers="malformed")
    stopped.assert_awaited_once()
    sdk.assert_not_called()
