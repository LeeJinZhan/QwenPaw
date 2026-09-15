import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from bank_runtime.model_timing import ModelTimingCollector,current_collector,observed_model_handler


def test_first_generated_output_ignores_empty_chunks_and_preserves_objects():
    async def run():
        collector=ModelTimingCollector(); token=current_collector.set(collector)
        chunks=[SimpleNamespace(content=[]),SimpleNamespace(content=[SimpleNamespace(text='hello')]),SimpleNamespace(content=[SimpleNamespace(text='hello world')])]
        async def handler():
            async def stream():
                for chunk in chunks:yield chunk
            return stream()
        clock=iter([10,10.25])
        try:
            stream=await observed_model_handler(handler,clock=lambda:next(clock))()
            actual=[chunk async for chunk in stream]
            assert all(a is b for a,b in zip(chunks,actual))
            assert collector.records[0]['first_token_ms']==250
            assert collector.records[0]['status']=='observed'
            assert 'hello' not in str(collector.records)
        finally:current_collector.reset(token)
    asyncio.run(run())


def test_non_streaming_and_empty_output_are_unknown_not_zero():
    async def run():
        collector=ModelTimingCollector(); token=current_collector.set(collector)
        async def direct():return SimpleNamespace(content=[SimpleNamespace(text='text')])
        async def empty():
            async def stream():
                if False:yield None
            return stream()
        try:
            await observed_model_handler(direct)()
            stream=await observed_model_handler(empty)(); assert [x async for x in stream]==[]
            assert [r['status'] for r in collector.records]==['non_streaming','no_output']
            assert all(r['first_token_ms'] is None for r in collector.records)
        finally:current_collector.reset(token)
    asyncio.run(run())


def test_failed_call_is_not_retried_by_observer():
    async def run():
        collector=ModelTimingCollector(); token=current_collector.set(collector)
        async def failed():raise ValueError('private detail')
        try:
            with pytest.raises(ValueError):await observed_model_handler(failed)()
            assert collector.calls==1 and collector.records[0]['status']=='failed'
            assert 'private' not in str(collector.records)
        finally:current_collector.reset(token)
    asyncio.run(run())


def test_producer_scopes_calls_and_emits_metadata_before_terminal():
    import json
    from bank_runtime.events import project_sse_stream
    async def run_one(task_id):
        async def source():
            async def handler():
                async def chunks():yield SimpleNamespace(content=[SimpleNamespace(text='private model content')])
                return chunks()
            response=await observed_model_handler(handler)()
            async for _ in response:pass
            yield 'data: {"event":"answer.completed"}\n\n'
        events=[json.loads(item.removeprefix('data: ').strip()) async for item in project_sse_stream(source(),task_id)]
        timing=[item for item in events if item['event']=='model.timing']
        assert len(timing)==1 and timing[0]['runtime_task_id']==task_id
        assert events.index(timing[0])<next(i for i,item in enumerate(events) if item['event']=='answer.completed')
        assert 'private model content' not in str(events)
        assert current_collector.get() is None
        return timing[0]['model_call_id']
    async def run():
        first,second=await asyncio.gather(run_one('one'),run_one('two'))
        assert first!=second
    asyncio.run(run())


def test_collection_limit_does_not_limit_model_execution():
    async def run():
        collector=ModelTimingCollector(limit=1);token=current_collector.set(collector)
        async def handler():return 'response'
        try:
            assert await observed_model_handler(handler)()=='response'
            assert await observed_model_handler(handler)()=='response'
            assert collector.calls==2 and collector.omitted_calls==1 and len(collector.records)==1
        finally:current_collector.reset(token)
    asyncio.run(run())
