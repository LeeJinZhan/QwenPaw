"""Project trusted task identity into invocation-scoped MCP metadata."""
from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from contextlib import nullcontext
from copy import deepcopy
from typing import Any

from qwenpaw.drivers.mcp_context import mcp_call_metadata, mcp_call_timeout
from .personalization import TrustedBankIdentity


def caller_context(identity: TrustedBankIdentity) -> dict[str, Any]:
    """Only the agreed public caller fields cross the MCP service boundary."""
    return {
        "user_id": identity.user_id,
        "display_name": identity.display_name,
        "role_codes": sorted(identity.roles),
        "role_names": list(identity.role_names),
        "org_id": identity.org_id,
        "org_name": identity.org_name,
        "assistant_code": identity.assistant_code,
        "assistant_name": identity.assistant_name,
    }


async def stream_with_mcp_metadata(
    next_handler: Callable[[], AsyncGenerator],
    metadata: dict[str, Any],
    remaining_seconds: Callable[[], float | None] | None = None,
) -> AsyncGenerator:
    """Reset before yielding; a consumer may close the stream in another task."""
    def budget():
        return mcp_call_timeout(remaining_seconds()) if remaining_seconds else nullcontext()

    with mcp_call_metadata(deepcopy(metadata)), budget():
        stream = next_handler()
    try:
        while True:
            with mcp_call_metadata(deepcopy(metadata)), budget():
                try:
                    item = await anext(stream)
                except StopAsyncIteration:
                    break
            yield item
    finally:
        # Cleanup must still run after expiry. No new transport timeout here.
        with mcp_call_metadata(deepcopy(metadata)):
            await stream.aclose()
