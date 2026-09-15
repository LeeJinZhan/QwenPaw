"""Per-model-call SDK observation clocks; never retain model input or output."""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from time import monotonic
from uuid import uuid4


@dataclass
class ModelTimingCollector:
    records: list[dict] = field(default_factory=list)
    calls: int = 0
    limit: int = 128
    omitted_calls: int = 0

    def drain(self):
        records, self.records = self.records, []
        return records


current_collector: ContextVar[ModelTimingCollector | None] = ContextVar('bank_model_timing', default=None)


def _generated_output(chunk):
    for block in getattr(chunk, 'content', ()) or ():
        # Empty role/usage/control chunks are not evidence of a generated token.
        for key in ('text', 'thinking'):
            value = getattr(block, key, None)
            if isinstance(value, str) and value:
                return True
        arguments = getattr(block, 'input', None)
        if isinstance(arguments, (str, dict)) and arguments:
            return True
    return False


def observed_model_handler(handler, *, clock=monotonic):
    async def call(**kwargs):
        collector = current_collector.get()
        if collector is None:
            return await handler(**kwargs)
        collector.calls += 1
        if collector.calls > collector.limit:
            collector.omitted_calls += 1
            return await handler(**kwargs)
        call_id = uuid4().hex
        started = clock()
        first_ms = None
        recorded = False

        def record(status):
            nonlocal recorded
            if not recorded:
                recorded = True
                collector.records.append({'model_call_id': call_id, 'status': status,
                    'first_token_ms': first_ms, 'basis': 'sdk_first_generated_output',
                    'clock': 'monotonic'})
        try:
            response = await handler(**kwargs)
        except BaseException:
            record('failed')
            raise
        if not hasattr(response, '__aiter__'):
            record('non_streaming')
            return response

        async def stream():
            nonlocal first_ms
            iterator = response.__aiter__()
            try:
                async for chunk in iterator:
                    if first_ms is None and _generated_output(chunk):
                        first_ms = max(0.0, round((clock()-started)*1000, 3))
                        record('observed')
                    yield chunk
            except BaseException:
                record('interrupted')
                raise
            else:
                record('observed' if first_ms is not None else 'no_output')
            finally:
                close = getattr(iterator, 'aclose', None)
                if close is not None:
                    await close()
        return stream()
    return call
