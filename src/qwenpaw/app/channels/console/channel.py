# -*- coding: utf-8 -*-
# pylint: disable=too-many-branches,too-many-statements
"""Console Channel.

A lightweight channel that prints all agent responses to stdout.

Messages are sent to the agent via the standard AgentApp ``/agent/process``
endpoint or via POST /console/chat. This channel handles the **output** side:
whenever a completed message event or a proactive send arrives, it is
pretty-printed to the terminal.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional, Union

from agentscope_runtime.engine.schemas.agent_schemas import (
    MessageType,
    Message,
    RunStatus,
)

from ....agents.tools.runtime_sandbox_oss import (
    RuntimeAttachmentPreparationError,
)
from ....config.config import ConsoleConfig as ConsoleChannelConfig
from ...console_push_store import append as push_store_append
from ....constant import DEFAULT_MEDIA_DIR
from ....exceptions import ModelQuotaExceededException
from .runtime_event_projection import (
    RuntimeEventProjector,
    should_project_runtime_events,
)
from ..base import (
    BaseChannel,
    AudioContent,
    ContentType,
    FileContent,
    ImageContent,
    OnReplySent,
    OutgoingContentPart,
    ProcessHandler,
    VideoContent,
    TextContent,
)
from ..utils import file_url_to_local_path


logger = logging.getLogger(__name__)

# ANSI colour helpers (degrade gracefully if not a tty)
_USE_COLOR = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()

_GREEN = "\033[32m" if _USE_COLOR else ""
_YELLOW = "\033[33m" if _USE_COLOR else ""
_RED = "\033[31m" if _USE_COLOR else ""
_BOLD = "\033[1m" if _USE_COLOR else ""
_RESET = "\033[0m" if _USE_COLOR else ""
_RUNTIME_PROJECTION_TICK = object()
_CLEANUP_TIMEOUT_SECONDS = 1.0
_CLEANUP_CANCEL_GRACE_SECONDS = 0.1


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _log_cleanup_failure(message: str, error: BaseException) -> None:
    logger.warning(
        message,
        exc_info=(type(error), error, error.__traceback__),
    )


def _observe_late_cleanup(
    cleanup_task: asyncio.Future,
    description: str,
) -> None:
    try:
        cleanup_task.result()
    except asyncio.CancelledError:
        return
    except Exception as error:  # pylint: disable=broad-exception-caught
        _log_cleanup_failure(f"{description} failed", error)


async def _await_protected_cleanup(
    cleanup_task: asyncio.Future,
    description: str,
) -> Any:
    caller_cancelled: asyncio.CancelledError | None = None
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _CLEANUP_TIMEOUT_SECONDS
    timed_out = False

    while not cleanup_task.done():
        remaining = deadline - loop.time()
        if remaining <= 0:
            timed_out = True
            break
        try:
            done, _ = await asyncio.wait(
                {cleanup_task},
                timeout=remaining,
            )
        except asyncio.CancelledError as error:
            caller_cancelled = error
            continue
        if not done:
            timed_out = True
            break

    if timed_out and not cleanup_task.done():
        logger.warning("%s timed out; cancelling cleanup", description)
        cleanup_task.cancel()
        cancel_deadline = loop.time() + _CLEANUP_CANCEL_GRACE_SECONDS
        while not cleanup_task.done():
            remaining = cancel_deadline - loop.time()
            if remaining <= 0:
                break
            try:
                done, _ = await asyncio.wait(
                    {cleanup_task},
                    timeout=remaining,
                )
            except asyncio.CancelledError as error:
                caller_cancelled = error
                continue
            if not done:
                break

    result = None
    if cleanup_task.done():
        try:
            result = cleanup_task.result()
        except asyncio.CancelledError as error:
            if not timed_out:
                _log_cleanup_failure(f"{description} failed", error)
        except Exception as error:  # pylint: disable=broad-exception-caught
            _log_cleanup_failure(f"{description} failed", error)
    else:
        logger.error("%s remains pending after cancellation", description)
        cleanup_task.add_done_callback(
            lambda task: _observe_late_cleanup(task, description),
        )

    if caller_cancelled is not None:
        raise caller_cancelled
    return result


async def _gather_pending_native_event(
    pending: asyncio.Task,
) -> None:
    results = await asyncio.gather(pending, return_exceptions=True)
    for result in results:
        if isinstance(result, asyncio.CancelledError):
            continue
        if isinstance(result, BaseException):
            _log_cleanup_failure(
                "runtime pending native event cleanup failed",
                result,
            )


async def _cancel_pending_native_event(
    pending: asyncio.Task | None,
) -> None:
    if pending is None:
        return
    if not pending.done():
        pending.cancel()
    cleanup_task = asyncio.create_task(
        _gather_pending_native_event(pending),
    )
    await _await_protected_cleanup(
        cleanup_task,
        "runtime pending native event gather",
    )


async def _aclose_safely(iterator: Any, description: str) -> None:
    close = getattr(iterator, "aclose", None)
    if not callable(close):
        return
    try:
        cleanup_task = asyncio.ensure_future(close())
    except Exception as error:  # pylint: disable=broad-exception-caught
        _log_cleanup_failure(
            f"{description} aclose failed",
            error,
        )
        return
    await _await_protected_cleanup(
        cleanup_task,
        f"{description} aclose",
    )


async def _with_runtime_projection_ticks(
    events: Any,
    *,
    projector: RuntimeEventProjector,
) -> AsyncGenerator[Any, None]:
    """Yield native events plus flush ticks without cancelling the producer."""
    iterator = aiter(events)
    pending: asyncio.Task | None = None
    try:
        while True:
            if pending is None:
                pending = asyncio.create_task(anext(iterator))
            flush_delay = projector.next_flush_delay_seconds()
            if flush_delay is None:
                done, _ = await asyncio.wait({pending})
            else:
                done, _ = await asyncio.wait(
                    {pending},
                    timeout=flush_delay,
                )
            if not done:
                yield _RUNTIME_PROJECTION_TICK
                continue

            completed = pending
            pending = None
            try:
                event = completed.result()
            except StopAsyncIteration:
                break
            yield event
            if projector.terminal_sent:
                break
    finally:
        caller_cancelled: asyncio.CancelledError | None = None
        try:
            await _cancel_pending_native_event(pending)
        except asyncio.CancelledError as error:
            caller_cancelled = error
        try:
            await _aclose_safely(
                iterator,
                "runtime native event iterator",
            )
        except asyncio.CancelledError as error:
            caller_cancelled = caller_cancelled or error
        if caller_cancelled is not None:
            raise caller_cancelled


class ConsoleChannel(BaseChannel):
    """Console Channel: prints agent responses to stdout.

    Input is handled by AgentApp's ``/agent/process`` endpoint; this
    channel only takes care of output (printing to the terminal).

    Supports filtering options via config:
        - show_tool_details: Display tool execution details
        - filter_tool_messages: Hide intermediate tool messages
        - filter_thinking: Hide agent thinking/reasoning blocks
    """

    channel = "console"

    def __init__(
        self,
        process: ProcessHandler,
        enabled: bool,
        bot_prefix: str,
        on_reply_sent: OnReplySent = None,
        show_tool_details: bool = True,
        filter_tool_messages: bool = False,
        filter_thinking: bool = False,
        workspace_dir: Optional[Union[str, Path]] = None,
        media_dir: Optional[str] = None,
    ):
        """Initialize ConsoleChannel.

        Args:
            process: Handler for agent requests.
            enabled: Whether this channel is active.
            bot_prefix: Prefix string for bot messages.
            on_reply_sent: Callback when reply is sent.
            show_tool_details: Whether to show tool execution details.
            filter_tool_messages: Whether to filter out tool messages.
            filter_thinking: Whether to filter thinking/reasoning blocks.
            workspace_dir: Agent workspace directory; used to resolve uploaded
                file names (media_dir = workspace_dir / "media").
            media_dir: Agent workspace directory for resolving uploads.
        """
        super().__init__(
            process,
            on_reply_sent=on_reply_sent,
            show_tool_details=show_tool_details,
            filter_tool_messages=filter_tool_messages,
            filter_thinking=filter_thinking,
        )
        self.enabled = enabled
        self.bot_prefix = bot_prefix
        self._workspace_dir = (
            Path(workspace_dir).expanduser() if workspace_dir else None
        )

        # Use workspace-specific media dir if workspace_dir is provided
        if not media_dir and self._workspace_dir:
            self._media_dir = self._workspace_dir / "media"
        elif media_dir:
            self._media_dir = Path(media_dir).expanduser()
        else:
            self._media_dir = DEFAULT_MEDIA_DIR
        self._media_dir.mkdir(parents=True, exist_ok=True)

        # Windows stdout encoding fix
        if sys.platform == "win32":
            try:
                sys.stdout.reconfigure(encoding="utf-8", errors="replace")
                sys.stderr.reconfigure(encoding="utf-8", errors="replace")
            except Exception as e:
                logger.debug(
                    "Failed to reconfigure stdout encoding on Windows: %s",
                    e,
                )

    @property
    def media_dir(self) -> Path:
        """Media directory"""
        return self._media_dir

    @classmethod
    def from_env(
        cls,
        process: ProcessHandler,
        on_reply_sent: OnReplySent = None,
    ) -> "ConsoleChannel":
        return cls(
            process=process,
            enabled=os.getenv("CONSOLE_CHANNEL_ENABLED", "1") == "1",
            bot_prefix=os.getenv("CONSOLE_BOT_PREFIX", ""),
            on_reply_sent=on_reply_sent,
            media_dir=os.getenv("CONSOLE_MEDIA_DIR", ""),
        )

    @classmethod
    def from_config(
        cls,
        process: ProcessHandler,
        config: ConsoleChannelConfig,
        on_reply_sent: OnReplySent = None,
        show_tool_details: bool = True,
        filter_tool_messages: bool = False,
        filter_thinking: bool = False,
        workspace_dir: Optional[Union[str, Path]] = None,
    ) -> "ConsoleChannel":
        """Create ConsoleChannel from config.

        Args:
            process: Handler for agent requests.
            config: Console channel configuration.
            on_reply_sent: Callback when reply is sent.
            show_tool_details: Whether to show tool execution details.
            filter_tool_messages: Whether to filter out tool messages.
            filter_thinking: Whether to filter thinking/reasoning blocks.
            workspace_dir: Agent workspace directory for resolving uploads.

        Returns:
            Configured ConsoleChannel instance.
        """
        return cls(
            process=process,
            enabled=config.enabled,
            bot_prefix=config.bot_prefix or "",
            on_reply_sent=on_reply_sent,
            show_tool_details=show_tool_details,
            filter_tool_messages=filter_tool_messages,
            filter_thinking=filter_thinking,
            workspace_dir=workspace_dir,
            media_dir=config.media_dir or "",
        )

    def resolve_session_id(
        self,
        sender_id: str,
        channel_meta: Optional[dict] = None,
    ) -> str:
        """Resolve session_id: use explicit meta['session_id'] when provided
        (e.g. from the HTTP /console/chat API), otherwise fall back to
        'console:<sender_id>'.
        """
        if channel_meta and channel_meta.get("session_id"):
            return channel_meta["session_id"]
        return f"{self.channel}:{sender_id}"

    def _resolve_console_upload_refs(
        self,
        content_parts: List[Any],
    ) -> List[Any]:
        """Resolve Image/File/Audio/VideoContent."""
        if not self._media_dir:
            return content_parts

        def resolve_one(part: Any) -> Optional[OutgoingContentPart]:
            if isinstance(part, dict):
                content_type = part.get("type")
                if content_type == ContentType.TEXT:
                    return TextContent(
                        type=ContentType.TEXT,
                        text=str(part.get("text") or ""),
                    )
            content_type = getattr(part, "type", None)
            if content_type == ContentType.IMAGE:
                url = getattr(part, "image_url", None)
                if url:
                    return ImageContent(
                        type=ContentType.IMAGE,
                        image_url=url,
                    )
            elif content_type == ContentType.VIDEO:
                url = getattr(part, "video_url", None)
                if url:
                    return VideoContent(
                        type=ContentType.VIDEO,
                        video_url=url,
                    )
            elif content_type == ContentType.AUDIO:
                url = getattr(part, "data", None)
                if url:
                    return AudioContent(
                        type=ContentType.AUDIO,
                        data=url,
                    )
            elif content_type == ContentType.FILE:
                url = getattr(part, "file_url", None)
                if url:
                    return FileContent(
                        type=ContentType.FILE,
                        filename=getattr(part, "filename", None)
                        or Path(url).name,
                        file_url=url,
                    )
            elif content_type == ContentType.TEXT:
                return TextContent(type=ContentType.TEXT, text=part.text)
            return part

        input_content_parts = []
        for content in content_parts:
            part = resolve_one(content)
            if part is not None:
                input_content_parts.append(part)
        return input_content_parts

    def build_agent_request_from_native(self, native_payload: Any) -> Any:
        """
        Build AgentRequest from console native payload (dict with
        channel_id, sender_id, content_parts, meta). content_parts are
        runtime Content types.
        """
        payload = native_payload if isinstance(native_payload, dict) else {}
        channel_id = payload.get("channel_id") or self.channel
        sender_id = payload.get("sender_id") or ""
        content_parts = payload.get("content_parts") or []
        meta = payload.get("meta") or {}
        session_id = self.resolve_session_id(sender_id, meta)
        request = self.build_agent_request_from_user_content(
            channel_id=channel_id,
            sender_id=sender_id,
            session_id=session_id,
            content_parts=content_parts,
            channel_meta=meta,
        )
        request.channel_meta = meta
        return request

    async def _extract_media_message(self, message: Message) -> Message | None:
        """Extract media message from message."""
        parts = self._message_to_content_parts(message)
        media_message = None
        if message.type in (
            MessageType.FUNCTION_CALL_OUTPUT,
            MessageType.PLUGIN_CALL_OUTPUT,
            MessageType.MCP_TOOL_CALL_OUTPUT,
        ):
            new_parts = []
            for part in parts:
                if part.type == ContentType.IMAGE:
                    new_part = copy.deepcopy(part)
                    new_part.image_url = file_url_to_local_path(
                        new_part.image_url,
                    )
                    new_parts.append(new_part)
                elif part.type == ContentType.VIDEO:
                    new_part = copy.deepcopy(part)
                    new_part.video_url = file_url_to_local_path(
                        new_part.video_url,
                    )
                    new_parts.append(new_part)
                elif part.type == ContentType.AUDIO:
                    new_part = copy.deepcopy(part)
                    new_part.data = file_url_to_local_path(new_part.data)
                    new_parts.append(new_part)
                elif part.type == ContentType.FILE:
                    new_part = copy.deepcopy(part)
                    new_part.file_url = file_url_to_local_path(
                        new_part.file_url,
                    )
                    new_parts.append(new_part)
            if new_parts:
                media_message = Message(
                    type=MessageType.MESSAGE,
                    role="assistant",
                    content=new_parts,
                )
        return media_message

    def _build_trailing_usage_sse(self, session_id: str) -> str | None:
        """Return one trailing turn_usage SSE block for the console UI."""
        from ....token_usage import get_pending_usage_for_stream

        turn, ctx = get_pending_usage_for_stream(session_id)
        if turn is None and ctx is None:
            return None

        if turn:
            logger.info("Usage for session %s: %s", session_id, turn)
            if ctx:
                self._print_status_line(turn, ctx)

        payload: Dict[str, Any] = {
            "type": "turn_usage",
            "session_id": session_id,
            "usage": turn,
            "context_usage": ctx,
        }
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    def _print_status_line(
        self,
        turn: Dict[str, Any],
        ctx: Dict[str, Any],
    ) -> None:
        """Print a one-line terminal summary of turn + context usage."""
        from ....token_usage import fmt_tokens

        pt = turn.get("prompt_tokens", 0)
        ct = turn.get("completion_tokens", 0)
        tt = turn.get("total_tokens", 0)
        est = int(ctx.get("estimated_tokens", 0) or 0)
        mx = int(ctx.get("max_input_length", 0) or 0)
        ratio = ctx.get("context_usage_ratio", 0) or 0
        turn_line = (
            f"{_GREEN}Turn {_BOLD}{fmt_tokens(tt)}{_RESET} "
            f"(in {fmt_tokens(pt)} · out {fmt_tokens(ct)})"
        )
        ctx_line = (
            f" · Context {_BOLD}{fmt_tokens(est)}{_RESET} / "
            f"{fmt_tokens(mx)} ({ratio:.1f}%)"
        )
        self._safe_print(f"📝 {turn_line}{ctx_line}")

    async def stream_one(self, payload: Any) -> AsyncGenerator[str, None]:
        """Process one payload and yield SSE-formatted events"""
        if isinstance(payload, dict) and "content_parts" in payload:
            session_id = self.resolve_session_id(
                payload.get("sender_id") or "",
                payload.get("meta"),
            )
            content_parts = self._resolve_console_upload_refs(
                payload.get("content_parts") or [],
            )
            should_process, merged = self._apply_no_text_debounce(
                session_id,
                content_parts,
            )
            if not should_process:
                return
            payload = {**payload, "content_parts": merged}
            request = self.build_agent_request_from_native(payload)
        else:
            request = payload
            session_id = getattr(request, "session_id", "") or ""
            if getattr(request, "input", None):
                contents = list(
                    getattr(request.input[0], "content", None) or [],
                )
                should_process, merged = self._apply_no_text_debounce(
                    session_id,
                    contents,
                )
                if not should_process:
                    return
                if merged and hasattr(request.input[0], "content"):
                    request.input[0].content = merged
        session_id = getattr(request, "session_id", "") or session_id
        user_id = getattr(request, "user_id", "") or ""
        channel_name = getattr(request, "channel", "") or self.channel
        is_runtime_request = False
        runtime_projector: RuntimeEventProjector | None = None
        events: Any = None
        try:
            from ....token_usage import (
                finalize_console_turn_usage,
                reset_pending_usage_for_stream,
            )

            reset_pending_usage_for_stream(session_id)
            send_meta = getattr(request, "channel_meta", None) or {}
            send_meta.setdefault("bot_prefix", self.bot_prefix)
            is_runtime_request = should_project_runtime_events(
                getattr(request, "channel", None),
                send_meta,
            )
            if is_runtime_request:
                runtime_projector = RuntimeEventProjector.from_environment()
            last_response = None
            event_count = 0
            runtime_terminal_sent = False

            native_events = self._process(request)
            events = (
                _with_runtime_projection_ticks(
                    native_events,
                    projector=runtime_projector,
                )
                if runtime_projector is not None
                else native_events
            )
            async for event in events:
                if event is _RUNTIME_PROJECTION_TICK:
                    for projected in runtime_projector.flush_due():
                        data = json.dumps(projected, ensure_ascii=False)
                        yield f"data: {data}\n\n"
                    continue
                event_count += 1
                obj = getattr(event, "object", None)
                status = getattr(event, "status", None)
                ev_type = getattr(event, "type", None)

                logger.debug(
                    "console event #%s: object=%s status=%s type=%s",
                    event_count,
                    obj,
                    status,
                    ev_type,
                )

                if (
                    runtime_projector is None
                    and event.object == "response"
                    and event.status == RunStatus.Completed
                ):
                    event_output = event.output
                    event.output = []
                    if event_output is not None:
                        for message in event_output:
                            event.output.append(message)
                            media_message = await self._extract_media_message(
                                message,
                            )
                            if media_message:
                                event.output.append(media_message)

                if runtime_projector is not None:
                    projected_events = runtime_projector.project(event)
                    for projected in projected_events:
                        data = json.dumps(projected, ensure_ascii=False)
                        yield f"data: {data}\n\n"
                    if runtime_projector.terminal_sent:
                        if obj == "response":
                            last_response = event
                        break
                else:
                    data = self._serialize_event_for_sse(event)
                    yield f"data: {data}\n\n"
                    if obj == "response" and status == RunStatus.Completed:
                        runtime_terminal_sent = True

                if (
                    obj == "message"
                    and status == RunStatus.Completed
                ):
                    if runtime_projector is None:
                        media_message = await self._extract_media_message(event)
                        if media_message:
                            media_json = self._serialize_event_for_sse(
                                media_message,
                            )
                            yield f"data: {media_json}\n\n"
                    parts = self._message_to_content_parts(event)
                    self._print_parts(parts, ev_type)

                elif obj == "response":
                    last_response = event

            runner = getattr(self._workspace, "runner", None)
            session = getattr(runner, "session", None) if runner else None
            agent_id = (
                getattr(self._workspace, "agent_id", "default")
                if self._workspace is not None
                else "default"
            )
            if session is not None and session_id:
                await finalize_console_turn_usage(
                    session=session,
                    session_id=session_id,
                    user_id=user_id,
                    channel=channel_name,
                    agent_id=agent_id,
                )

            if trailing := self._build_trailing_usage_sse(session_id):
                yield trailing

            logger.info(
                "console stream done: event_count=%s has_response=%s",
                event_count,
                last_response is not None,
            )

            err_msg = self._get_response_error_message(last_response)
            if err_msg:
                self._print_error(err_msg)
            if runtime_projector is not None:
                for projected in runtime_projector.finish(
                    success=not bool(err_msg),
                ):
                    data = json.dumps(projected, ensure_ascii=False)
                    yield f"data: {data}\n\n"
            elif is_runtime_request and not runtime_terminal_sent:
                completed_payload = {
                    "event": "completed",
                    "chat_id": session_id,
                }
                data = json.dumps(completed_payload, ensure_ascii=False)
                yield f"data: {data}\n\n"

            to_handle = request.user_id or ""
            if self._on_reply_sent:
                self._on_reply_sent(
                    self.channel,
                    to_handle,
                    request.session_id or f"{self.channel}:{to_handle}",
                )

        except RuntimeAttachmentPreparationError as e:
            logger.warning(
                "runtime attachment preparation failed reason_code=%s",
                e.reason_code,
            )
            err_msg = "附件读取失败"
            self._print_error(err_msg)
            if runtime_projector is not None:
                for projected in runtime_projector.finish(
                    success=False,
                    message=err_msg,
                ):
                    data = json.dumps(projected, ensure_ascii=False)
                    yield f"data: {data}\n\n"
            elif is_runtime_request:
                failed_payload = {
                    "event": "failed",
                    "status": "failed",
                    "chat_id": session_id,
                    "error": err_msg,
                    "reason_code": e.reason_code,
                }
                data = json.dumps(failed_payload, ensure_ascii=False)
                yield f"data: {data}\n\n"
        except ModelQuotaExceededException as e:
            logger.warning("rate limit hit: %s", e)
            alternatives = self._get_free_model_alternatives()
            rl_event = json.dumps(
                {
                    "type": "rate_limited",
                    "error": str(e).strip(),
                    "alternatives": alternatives,
                },
            )
            yield f"data: {rl_event}\n\n"
            self._print_error(str(e).strip())
        except Exception as e:
            logger.exception("console process/reply failed")
            err_msg = str(e).strip() or "An error occurred while processing."
            self._print_error(err_msg)
            if runtime_projector is not None:
                error_code = (
                    "RUNTIME_SESSION_NOT_FOUND"
                    if "RUNTIME_SESSION_NOT_FOUND" in err_msg
                    else None
                )
                for projected in runtime_projector.finish(
                    success=False,
                    error_code=error_code,
                ):
                    data = json.dumps(projected, ensure_ascii=False)
                    yield f"data: {data}\n\n"
        finally:
            await _aclose_safely(events, "console nested event stream")

    async def consume_one(self, payload: Any) -> None:
        """Process one payload; drain stream_one (queue/terminal)."""
        async for _ in self.stream_one(payload):
            pass

    # ── pretty-print helpers ────────────────────────────────────────

    def _safe_print(self, text: str) -> None:
        """Safely print text, handling Windows encoding and pipe issues.

        On Windows, print() can raise OSError [Errno 22] when output is
        piped or contains unsupported characters. This wrapper handles
        such cases gracefully.
        """
        try:
            print(text)
        except OSError as e:
            if e.errno == 22:
                logger.warning(
                    "Print failed with OSError [Errno 22], attempting "
                    "fallback encoding",
                )
                try:
                    sys.stdout.buffer.write(
                        text.encode("utf-8", errors="replace"),
                    )
                    sys.stdout.buffer.write(b"\n")
                    sys.stdout.buffer.flush()
                except Exception as fallback_err:
                    logger.error(
                        "Failed to print even with fallback: %s",
                        fallback_err,
                    )
            else:
                logger.error("Print failed with OSError: %s", e)

    def _print_parts(
        self,
        parts: List[OutgoingContentPart],
        ev_type: Optional[str] = None,
    ) -> None:
        """Print outgoing content parts to stdout."""
        ts = _ts()
        label = f" ({ev_type})" if ev_type else ""
        self._safe_print(
            f"\n{_GREEN}{_BOLD}🤖 [{ts}] Bot{label}{_RESET}",
        )
        for p in parts:
            t = getattr(p, "type", None)
            if t == ContentType.TEXT and getattr(p, "text", None):
                self._safe_print(f"{self.bot_prefix}{p.text}")
            elif t == ContentType.REFUSAL and getattr(p, "refusal", None):
                self._safe_print(f"{_RED}⚠ Refusal: {p.refusal}{_RESET}")
            elif t == ContentType.IMAGE and getattr(p, "image_url", None):
                self._safe_print(f"{_YELLOW}🖼  [Image: {p.image_url}]{_RESET}")
            elif t == ContentType.VIDEO and getattr(p, "video_url", None):
                self._safe_print(f"{_YELLOW}🎬 [Video: {p.video_url}]{_RESET}")
            elif t == ContentType.AUDIO and getattr(p, "data", None):
                self._safe_print(f"{_YELLOW}🔊 [Audio]{_RESET}")
            elif t == ContentType.FILE:
                url = (
                    getattr(p, "file_url", None)
                    or getattr(p, "file_id", None)
                    or ""
                )
                self._safe_print(f"{_YELLOW}📎 [File: {url}]{_RESET}")
        self._safe_print("")

    def _get_free_model_alternatives(self) -> list:
        """Return a list of alternative free models."""
        try:
            from ....providers.provider_manager import (
                ProviderManager,
            )

            pm = ProviderManager.get_instance()
            if pm is None:
                return []
            alternatives = []
            all_providers = list(
                pm.builtin_providers.values(),
            ) + list(pm.custom_providers.values())
            for p in all_providers:
                meta = getattr(p, "meta", None) or {}
                if not meta.get("is_free_tier"):
                    continue
                for m in p.models:
                    if getattr(m, "is_free", False):
                        alternatives.append(
                            {
                                "provider_id": p.id,
                                "provider_name": p.name,
                                "model_id": m.id,
                                "model_name": m.name or m.id,
                            },
                        )
            return alternatives[:8]
        except Exception:
            return []

    def _print_error(self, err: str) -> None:
        ts = _ts()
        self._safe_print(
            f"\n{_RED}{_BOLD}❌ [{ts}] Error{_RESET}\n{_RED}{err}{_RESET}\n",
        )

    def _parts_to_text(
        self,
        parts: List[OutgoingContentPart],
        meta: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Merge parts to one body string (same logic as base send_content_parts).
        """
        text_parts: List[str] = []
        for p in parts:
            t = getattr(p, "type", None)
            if t == ContentType.TEXT and getattr(p, "text", None):
                text_parts.append(p.text or "")
            elif t == ContentType.REFUSAL and getattr(p, "refusal", None):
                text_parts.append(p.refusal or "")
        body = "\n".join(text_parts) if text_parts else ""
        prefix = (meta or {}).get("bot_prefix", self.bot_prefix) or ""
        if prefix and body:
            body = prefix + "  " + body
        return body

    # ── send (for proactive sends / cron) ───────────────────────────

    async def send(
        self,
        to_handle: str,
        text: str,
        meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Send a text message — prints to stdout and pushes to frontend."""
        if not self.enabled:
            return
        ts = _ts()
        prefix = (meta or {}).get("bot_prefix", self.bot_prefix) or ""
        self._safe_print(
            f"\n{_GREEN}{_BOLD}🤖 [{ts}] Bot → {to_handle}{_RESET}\n"
            f"{prefix}{text}\n",
        )
        sid = (meta or {}).get("session_id")
        if (
            sid
            and text.strip()
            and not (meta or {}).get("suppress_console_push")
        ):
            await push_store_append(sid, text.strip())

    async def send_content_parts(
        self,
        to_handle: str,
        parts: List[OutgoingContentPart],
        meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Send content parts — prints to stdout and pushes to frontend store.
        """
        self._print_parts(parts)
        sid = (meta or {}).get("session_id")
        if sid and not (meta or {}).get("suppress_console_push"):
            body = self._parts_to_text(parts, meta)
            if body.strip():
                await push_store_append(sid, body.strip())

    # ── lifecycle ───────────────────────────────────────────────────

    async def health_check(self) -> Dict[str, Any]:
        """Console channel is always healthy when enabled."""
        if not self.enabled:
            return {
                "channel": self.channel,
                "status": "disabled",
                "detail": "Console channel is disabled.",
            }
        return {
            "channel": self.channel,
            "status": "healthy",
            "detail": "Console channel is running.",
        }

    async def start(self) -> None:
        if not self.enabled:
            logger.debug("console channel disabled")
            return
        logger.info("Console channel started")

    async def stop(self) -> None:
        if not self.enabled:
            return
        logger.info("console channel stopped")
