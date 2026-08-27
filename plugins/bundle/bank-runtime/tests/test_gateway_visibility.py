from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from qwenpaw.drivers.adapters.agentscope_tool import DriverCapabilityTool
from qwenpaw.drivers.capabilities import CapabilityExposure, DriverCapability

from bank_runtime.gateway.hooks import BankRuntimeToolVisibilityHook
from bank_runtime.gateway.visibility import parse_runtime_tool_visibility


async def _never_invoke(_invocation):
    raise AssertionError("visibility tests must not invoke a Driver tool")


def _driver_tool(name: str, *, protocol: str = "mcp") -> DriverCapabilityTool:
    raw_name = name.split("__", 1)[-1]
    return DriverCapabilityTool(
        DriverCapability(
            capability_id=f"driver://{protocol}/mineru/tools/{raw_name}#invoke",
            driver_name="mineru",
            protocol=protocol,
            kind="tool",
            action="invoke",
            name=raw_name,
            exposure=CapabilityExposure(as_tool=True, tool_name=name),
        ),
        _never_invoke,
    )


def _context(visibility):
    ordinary = SimpleNamespace(name="runtime_sandbox_files_search")
    parse = _driver_tool("MinerU__parse_documents")
    chunks = _driver_tool("MinerU__read_document_chunks")
    request = SimpleNamespace(
        channel="bank-runtime",
        runtime_tool_visibility=visibility,
        runtime_tool_gateway={
            "capability_snapshot_hash": (
                str((visibility or {}).get("binding_snapshot_hash") or "").removeprefix(
                    "sha256:"
                )
            )
        },
    )
    agent = SimpleNamespace(
        toolkit=SimpleNamespace(
            tool_groups=[SimpleNamespace(tools=[ordinary, parse, chunks])]
        )
    )
    return SimpleNamespace(request=request, agent=agent), ordinary


def test_visibility_parser_accepts_only_non_authoritative_runtime_projection() -> None:
    parsed = parse_runtime_tool_visibility(
        {
            "worker_type": "qwenpaw",
            "worker_tool_names": ["MinerU__parse_documents"],
            "binding_snapshot_hash": f"sha256:{'a' * 64}",
            "authoritative": False,
        }
    )

    assert parsed is not None
    assert parsed.worker_tool_names == frozenset({"MinerU__parse_documents"})
    assert parse_runtime_tool_visibility({"authoritative": True}) is None
    assert parse_runtime_tool_visibility(None) is None


@pytest.mark.asyncio
async def test_visibility_hook_keeps_only_snapshot_driver_tools_and_trusted_tools() -> (
    None
):
    ctx, ordinary = _context(
        {
            "worker_type": "qwenpaw",
            "worker_tool_names": ["MinerU__parse_documents"],
            "binding_snapshot_hash": f"sha256:{'b' * 64}",
            "authoritative": False,
        }
    )

    await BankRuntimeToolVisibilityHook().run(ctx)

    assert ctx.agent.toolkit.tool_groups[0].tools[0] is ordinary
    assert [tool.name for tool in ctx.agent.toolkit.tool_groups[0].tools] == [
        "runtime_sandbox_files_search",
        "MinerU__parse_documents",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "visibility",
    [
        None,
        {},
        {
            "worker_type": "qwenpaw",
            "worker_tool_names": ["MinerU__parse_documents"],
            "binding_snapshot_hash": "invalid",
            "authoritative": False,
        },
        {
            "worker_type": "qwenpaw",
            "worker_tool_names": ["MinerU__parse_documents"],
            "binding_snapshot_hash": f"sha256:{'c' * 64}",
            "authoritative": False,
            "gateway_snapshot_hash": "mismatch",
        },
    ],
)
async def test_visibility_hook_fails_closed_for_missing_or_invalid_projection(
    visibility,
) -> None:
    projection = dict(visibility) if isinstance(visibility, dict) else visibility
    gateway_snapshot_hash = (
        projection.pop("gateway_snapshot_hash", "")
        if isinstance(projection, dict)
        else ""
    )
    ctx, ordinary = _context(projection)
    if gateway_snapshot_hash:
        ctx.request.runtime_tool_gateway["capability_snapshot_hash"] = (
            gateway_snapshot_hash
        )

    await BankRuntimeToolVisibilityHook().run(ctx)

    assert ctx.agent.toolkit.tool_groups[0].tools == [ordinary]


@pytest.mark.asyncio
async def test_visibility_hook_does_not_mutate_non_managed_channels() -> None:
    ctx, _ordinary = _context(None)
    ctx.request.channel = "console"
    before = list(ctx.agent.toolkit.tool_groups[0].tools)

    await BankRuntimeToolVisibilityHook().run(ctx)

    assert ctx.agent.toolkit.tool_groups[0].tools == before
