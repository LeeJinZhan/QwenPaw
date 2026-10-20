from datetime import timedelta
import pytest
from qwenpaw.drivers.mcp_context import current_mcp_timeout, mcp_call_timeout


def test_task_budget_is_local_and_bounded_to_one_hour():
    assert current_mcp_timeout() is None
    with mcp_call_timeout(3600):
        assert current_mcp_timeout() == timedelta(seconds=3600)
        with mcp_call_timeout(10000):
            assert current_mcp_timeout() == timedelta(seconds=3600)
        assert current_mcp_timeout() == timedelta(seconds=3600)
    assert current_mcp_timeout() is None


@pytest.mark.parametrize('seconds', [0, -1, float('nan'), float('inf')])
def test_expired_or_invalid_budget_does_not_change_shared_context(seconds):
    with pytest.raises(TimeoutError):
        with mcp_call_timeout(seconds):
            pass
    assert current_mcp_timeout() is None
