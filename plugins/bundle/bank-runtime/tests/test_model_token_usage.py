"""Real model-wrapper observations, independent of text or context estimates."""
import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace as NS

import pytest

from qwenpaw.token_usage import model_wrapper

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class NativeModel:
    model = "qwen-test"
    context_size = 32768

    def __init__(self, *responses):
        self.responses = iter(responses)

    async def __call__(self, **kwargs):
        response = next(self.responses)
        if isinstance(response, BaseException):
            raise response
        return response

    async def generate_structured_output(self, **kwargs):
        return await self(**kwargs)


def observe(callback):
    factory = getattr(model_wrapper, "observe_model_requests", None)
    assert callable(factory), "model requests need a scoped usage observer"
    return factory(callback)


def usage(prompt, completion):
    return NS(usage=NS(input_tokens=prompt, output_tokens=completion))


@pytest.mark.asyncio
async def test_two_model_calls_keep_both_receipts_not_only_last_session_usage():
    events = []
    model = model_wrapper.TokenRecordingModelWrapper("approved", NativeModel(usage(100, 20), usage(40, 10)))
    with observe(events.append):
        await model(messages=[])
        await model(messages=[])
    assert len(events) == 2
    assert len({item["request_id"] for item in events}) == 2
    assert sum(item["input_tokens"] for item in events) == 140
    assert sum(item["output_tokens"] for item in events) == 30
    assert all(item["provider_id"] == "approved" and item["model_id"] == "qwen-test" for item in events)
    assert all(item["usage_state"] == "reported" and item["terminal_status"] == "success" for item in events)
    assert all("messages" not in item and "content" not in item for item in events)


@pytest.mark.asyncio
async def test_stream_uses_last_cumulative_receipt_once_including_zero():
    async def stream():
        yield usage(10, 1)
        yield usage(10, 5)
    events = []
    model = model_wrapper.TokenRecordingModelWrapper("approved", NativeModel(stream(), usage(0, 0)))
    with observe(events.append):
        assert len([chunk async for chunk in await model(messages=[])]) == 2
        await model(messages=[])
    assert [(event["input_tokens"], event["output_tokens"]) for event in events] == [(10, 5), (0, 0)]
    assert all(event["usage_state"] == "reported" for event in events)


@pytest.mark.asyncio
async def test_missing_usage_and_failed_calls_stay_unknown_instead_of_zero():
    events = []
    model = model_wrapper.TokenRecordingModelWrapper("approved", NativeModel(NS(usage=None), RuntimeError("private error")))
    with observe(events.append):
        await model(messages=[])
        with pytest.raises(RuntimeError, match="private error"):
            await model(messages=[])
    assert [item["terminal_status"] for item in events] == ["success", "error"]
    assert all(item["usage_state"] == "unknown" and item["input_tokens"] is None
               and item["output_tokens"] is None for item in events)
    assert "private error" not in str(events)


@pytest.mark.asyncio
async def test_interrupted_stream_preserves_received_usage_without_swallowing_cancel():
    async def stream():
        yield usage(12, 3)
        raise asyncio.CancelledError()
    events = []
    model = model_wrapper.TokenRecordingModelWrapper("approved", NativeModel(stream()))
    with observe(events.append):
        with pytest.raises(asyncio.CancelledError):
            async for _ in await model(messages=[]):
                pass
    assert len(events) == 1
    assert events[0]["terminal_status"] == "cancelled"
    assert events[0]["input_tokens"] == 12


@pytest.mark.asyncio
async def test_scoped_observers_do_not_mix_concurrent_requests_and_failure_is_optional():
    first, second = [], []
    async def run(target, count):
        with observe(target.append):
            await asyncio.sleep(0)
            await model_wrapper.TokenRecordingModelWrapper("approved", NativeModel(usage(count, 1)))(messages=[])
    await asyncio.gather(run(first, 10), run(second, 20))
    assert [item["input_tokens"] for item in first] == [10]
    assert [item["input_tokens"] for item in second] == [20]
    def broken(event):
        raise RuntimeError("optional telemetry unavailable")
    with observe(broken):
        result = await model_wrapper.TokenRecordingModelWrapper("approved", NativeModel(usage(1, 2)))(messages=[])
    assert result.usage.output_tokens == 2


@pytest.mark.asyncio
async def test_structured_output_also_emits_a_reported_request():
    events = []
    model = model_wrapper.TokenRecordingModelWrapper("approved", NativeModel(usage(7, 8)))
    with observe(events.append):
        await model.generate_structured_output()
    assert len(events) == 1
    assert events[0]["input_tokens"] == 7


@pytest.mark.asyncio
async def test_bank_stream_attaches_trusted_task_and_trace_before_answer_terminal():
    import json
    from bank_runtime.events import project_sse_stream
    async def source():
        model = model_wrapper.TokenRecordingModelWrapper("approved", NativeModel(usage(11, 4)))
        await model(messages=[{"content": "private input"}])
        yield 'data: {"object":"response","status":"completed"}\n\n'
    events = [json.loads(line.removeprefix("data: ")) async for line in project_sse_stream(source(), "task-1", "trace-1")]
    receipts = [item["metadata"]["runtime_event"] for item in events if "metadata" in item]
    assert len(receipts) == 1
    assert receipts[0]["event_type"] == "model.request.completed"
    assert receipts[0]["task_id"] == "task-1"
    assert receipts[0]["trace_id"] == "trace-1"
    assert receipts[0]["input_tokens"] == 11
    assert "private input" not in str(events)
