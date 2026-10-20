import asyncio
from types import SimpleNamespace
import pytest
from qwenpaw.app.driver_config_service import DriverConfigService


@pytest.mark.asyncio
async def test_publication_waits_for_reload_before_policy_can_be_applied():
    service=DriverConfigService(SimpleNamespace())
    started,finish=asyncio.Event(),asyncio.Event()
    async def reload():
        started.set();await finish.wait()
    task=asyncio.create_task(reload())
    service._reload_tasks.add(task)
    task.add_done_callback(service._reload_tasks.discard)
    await started.wait()
    drain=asyncio.create_task(service.wait_for_reloads())
    await asyncio.sleep(0)
    assert not drain.done()
    finish.set();await drain
    assert not service._reload_tasks
