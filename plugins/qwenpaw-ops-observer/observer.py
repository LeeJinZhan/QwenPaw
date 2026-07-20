"""Public AgentScope middleware used by the Ops Observer Plugin."""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import UTC, datetime
from typing import Any, AsyncGenerator, Callable

from agentscope.middleware import MiddlewareBase
from agentscope.event import ThinkingBlockDeltaEvent, TextBlockDeltaEvent
from agentscope.tool import ToolResponse

from .storage import ObserverService


_FAILED_TOOL_STATES = {"error", "denied", "interrupted"}


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


class OpsObserverMiddleware(MiddlewareBase):
    """Observe one AgentScope reply without retaining its content.

    One instance is created per request by the plugin's middleware factory,
    so all counters and the run_id are naturally request-scoped and shared
    across the on_reply / on_reasoning / on_acting hooks.
    """

    def __init__(self, service: ObserverService, ctx: Any) -> None:
        self._service = service
        self._run_id = f"run-{uuid.uuid4().hex}"
        self._agent_id = str(ctx.agent_id)
        self._session_id = str(getattr(ctx, "session_id", "") or "")
        request = getattr(ctx, "request", None)
        self._channel = str(getattr(request, "channel", "") or "") if request is not None else ""
        self._started_at = _utc_now()
        self._started_monotonic = time.monotonic()
        self._tool_call_count = 0
        self._tool_error_count = 0
        self._llm_call_count = 0

    async def on_reply(
        self,
        agent: Any,
        input_kwargs: dict[str, Any],
        next_handler: Callable[..., AsyncGenerator[Any, None]],
    ) -> AsyncGenerator[Any, None]:
        status = "success"
        try:
            async for event in next_handler(**input_kwargs):
                yield event
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        except asyncio.TimeoutError:
            status = "timeout"
            raise
        except Exception:
            status = "error"
            raise
        finally:
            self._service.enqueue_run_summary({
                "schema_version": 2,
                "run_id": self._run_id,
                "agent_id": self._agent_id,
                "session_key": self._session_id,
                "channel": self._channel,
                "trigger_type": "chat",
                "started_at": self._started_at.isoformat().replace("+00:00", "Z"),
                "completed_at": _utc_now().isoformat().replace("+00:00", "Z"),
                "status": status,
                "duration_ms": int((time.monotonic() - self._started_monotonic) * 1000),
                "llm_call_count": self._llm_call_count,
                "tool_call_count": self._tool_call_count,
                "tool_error_count": self._tool_error_count,
                "output_artifact_count": self._output_artifact_count(agent),
                "error_category": {
                    "success": None,
                    "error": "execution_error",
                    "timeout": "timeout",
                    "cancelled": "cancelled",
                }[status],
                "config_ref": "config-qwenpaw-ops-observer",
            })

    async def on_reasoning(
        self,
        agent: Any,
        input_kwargs: dict[str, Any],
        next_handler: Callable[..., AsyncGenerator[Any, None]],
    ) -> AsyncGenerator[Any, None]:
        """Track per-LLM-call metrics: latency, time-to-first-token, chunk counts."""
        self._llm_call_count += 1
        call_seq = self._llm_call_count
        started_at = _utc_now()
        started_monotonic = time.monotonic()
        ttft_ms = -1
        thinking_chunks = 0
        text_chunks = 0
        status = "success"
        try:
            async for event in next_handler(**input_kwargs):
                if ttft_ms < 0:
                    ttft_ms = int((time.monotonic() - started_monotonic) * 1000)
                if isinstance(event, ThinkingBlockDeltaEvent):
                    thinking_chunks += 1
                elif isinstance(event, TextBlockDeltaEvent):
                    text_chunks += 1
                yield event
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        except Exception:
            status = "error"
            raise
        finally:
            self._service.enqueue_llm_call({
                "run_id": self._run_id,
                "call_seq": call_seq,
                "status": status,
                "started_at": started_at.isoformat().replace("+00:00", "Z"),
                "duration_ms": int((time.monotonic() - started_monotonic) * 1000),
                "ttft_ms": ttft_ms,
                "thinking_chunks": thinking_chunks,
                "text_chunks": text_chunks,
            })

    async def on_acting(
        self,
        agent: Any,
        input_kwargs: dict[str, Any],
        next_handler: Callable[..., AsyncGenerator[Any, None]],
    ) -> AsyncGenerator[Any, None]:
        """Track per-tool-call metrics: name, sequence, latency, outcome."""
        self._tool_call_count += 1
        tool_seq = self._tool_call_count
        tool_call = input_kwargs.get("tool_call") if isinstance(input_kwargs, dict) else None
        tool_name = getattr(tool_call, "name", None) or "unknown"
        started_at = _utc_now()
        started_monotonic = time.monotonic()
        status = "success"
        observed_response = False
        try:
            async for event in next_handler(**input_kwargs):
                if isinstance(event, ToolResponse):
                    observed_response = True
                    state = str(getattr(event.state, "value", event.state)).lower()
                    if state in _FAILED_TOOL_STATES:
                        status = state
                        self._tool_error_count += 1
                yield event
        except asyncio.CancelledError:
            status = "cancelled"
            self._tool_error_count += 1
            raise
        except Exception:
            status = "error"
            self._tool_error_count += 1
            raise
        finally:
            if not observed_response and status == "success":
                status = "error"
                self._tool_error_count += 1
            self._service.enqueue_tool_call({
                "run_id": self._run_id,
                "tool_seq": tool_seq,
                "tool_name": tool_name,
                "status": status,
                "started_at": started_at.isoformat().replace("+00:00", "Z"),
                "duration_ms": int((time.monotonic() - started_monotonic) * 1000),
            })

    @staticmethod
    def _output_artifact_count(agent: Any) -> int:
        context = list(getattr(getattr(agent, "state", None), "context", []) or [])
        if not context:
            return 0
        content = getattr(context[-1], "content", []) or []
        return sum(
            1 for block in content
            if str(getattr(block, "type", "")).lower() in {"image", "audio", "video", "file"}
        )
