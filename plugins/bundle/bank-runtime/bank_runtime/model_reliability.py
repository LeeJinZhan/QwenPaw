"""Bounded recovery of one model call, without replaying an agent/tool turn."""
from __future__ import annotations

import asyncio
from contextlib import suppress
from collections import Counter
from copy import copy
import json
import logging
import math
import os
import time

import httpx
import openai
from agentscope.message import AssistantMsg, SystemMsg, TextBlock, ThinkingBlock, ToolCallBlock
from agentscope.model import ChatResponse
from qwenpaw.exceptions import AgentRuntimeErrorException
from qwenpaw.providers.retry_scope import external_retry_owner

LOG = logging.getLogger(__name__)
MODEL_FAILURE_MESSAGES = {
    'MODEL_TIMEOUT': '模型响应超时，本次处理未完成。',
    'MODEL_OUTPUT_TRUNCATED': '模型输出达到长度限制，本次结果未能完整生成。',
    'MODEL_UPSTREAM_UNAVAILABLE': '模型服务暂时不可用，本次处理未完成。',
    'MODEL_REQUEST_REJECTED': '模型服务未接受本次请求。',
    'MODEL_CONTENT_FILTERED': '模型服务未返回可用结果。',
    'MODEL_EXECUTION_ERROR': '模型响应异常，本次处理未完成。',
    'WORKER_TIMEOUT': '本次任务已达到处理时限。',
}


def failure_code(error):
    details = getattr(error, 'details', None)
    reason = details.get('finish_reason') if isinstance(details, dict) else None
    if reason == 'length':
        return 'MODEL_OUTPUT_TRUNCATED'
    if reason == 'content_filter':
        return 'MODEL_CONTENT_FILTERED'
    code = getattr(error, 'error_code', '')
    if code in MODEL_FAILURE_MESSAGES:
        return code
    if isinstance(error, (TimeoutError, httpx.TimeoutException, openai.APITimeoutError)):
        return 'MODEL_TIMEOUT'
    if isinstance(error, (httpx.TransportError, openai.APIConnectionError)):
        return 'MODEL_UPSTREAM_UNAVAILABLE'
    status = getattr(error, 'status_code', 0)
    if isinstance(status, int) and (status == 429 or status >= 500):
        return 'MODEL_UPSTREAM_UNAVAILABLE'
    if isinstance(status, int) and 400 <= status < 500:
        return 'MODEL_REQUEST_REJECTED'
    return 'MODEL_EXECUTION_ERROR'


def positive_seconds(value, default, maximum=1800):
    try:
        number = float(value)
        if math.isfinite(number) and number > 0:
            return min(number, maximum)
    except (ValueError, TypeError):
        pass
    return default


class BankModelReliability:
    """One recovery allowance per bank task; the outer Runtime owns the deadline."""

    def __init__(self, budget_seconds=1800, *, idle_seconds=None,
                 no_output_retry_attempts=1, truncation_recovery_attempts=1):
        self.deadline = time.monotonic() + positive_seconds(budget_seconds, 1800)
        self.idle_seconds = positive_seconds(
            idle_seconds if idle_seconds is not None else os.getenv('BANK_MODEL_IDLE_TIMEOUT_SECONDS'), 180)
        self.no_output_retry = type(no_output_retry_attempts) is int and no_output_retry_attempts == 1
        self.truncation_recovery = type(truncation_recovery_attempts) is int and truncation_recovery_attempts == 1
        self.recovery_used = False
        self.failure = None

    def _error(self, code, *, phase='model', attempts=1):
        return AgentRuntimeErrorException(error_code=code, message=MODEL_FAILURE_MESSAGES[code],
            details={'phase': phase, 'attempts': attempts})

    async def _wait(self, awaitable, idle_deadline):
        remaining = min(self.deadline, idle_deadline) - time.monotonic()
        try:
            async with asyncio.timeout(max(0, remaining)):
                token = external_retry_owner.set(True)
                try:
                    return await awaitable
                finally:
                    external_retry_owner.reset(token)
        except TimeoutError:
            code = 'WORKER_TIMEOUT' if time.monotonic() >= self.deadline else 'MODEL_TIMEOUT'
            raise self._error(code) from None

    async def call(self, handler, *, allow_parameter_recovery=False, **kwargs):
        if self.failure is not None:
            raise self.failure
        return self._run(handler, kwargs, allow_parameter_recovery)

    async def _run(self, handler, request, allow_parameter_recovery):
        attempt = 0
        recovering = False
        prefix = ''
        original_id = ''
        expected_tools = []
        while True:
            attempt += 1
            started = time.monotonic()
            progress = False
            text = ''
            tool_calls = {}
            stream = None
            final = None
            response_id = ''
            idle_deadline = started + self.idle_seconds
            try:
                if started >= self.deadline:
                    raise self._error('WORKER_TIMEOUT')
                value = await self._wait(handler(**request), idle_deadline)
                if isinstance(value, ChatResponse):
                    async def single():
                        yield value
                    stream = single()
                else:
                    stream = value.__aiter__()
                while True:
                    try:
                        chunk = await self._wait(anext(stream), idle_deadline)
                    except StopAsyncIteration:
                        break
                    response_id = chunk.id
                    if str(chunk.finished_reason) == "interrupted":
                        raise asyncio.CancelledError()
                    has_progress = any(
                        (isinstance(b, TextBlock) and b.text)
                        or (isinstance(b, ThinkingBlock) and b.thinking)
                        or (isinstance(b, ToolCallBlock) and (b.name or b.input))
                        for b in chunk.content)
                    if has_progress:
                        progress = True
                        idle_deadline = time.monotonic() + self.idle_seconds
                    fragment = ''.join(b.text for b in chunk.content if isinstance(b, TextBlock))
                    text = fragment if chunk.is_last else text + fragment
                    tool_calls.update({b.id: b.name for b in chunk.content if isinstance(b, ToolCallBlock) and b.name})
                    if len(text) > 262144:
                        raise self._error('MODEL_OUTPUT_TRUNCATED')
                    if chunk.is_last:
                        final = chunk
                        # Final chunks are emitted only after a clean stream end.
                        continue
                    if not recovering:
                        # Incomplete tool parameters never reach the agent executor.
                        visible = copy(chunk)
                        visible.content = [b for b in chunk.content if not isinstance(b, ToolCallBlock)]
                        if visible.content:
                            yield visible
                if final is None:
                    raise self._error('MODEL_EXECUTION_ERROR')
                if recovering:
                    final = self._recovered_final(final, prefix, original_id, expected_tools)
                LOG.info('bank_model_call_completed attempt=%d elapsed_ms=%d recovered=%s',
                         attempt, int((time.monotonic()-started)*1000), recovering)
                yield final
                return
            except asyncio.CancelledError:
                raise
            except Exception as error:
                code = failure_code(error)
                LOG.warning('bank_model_call_failed code=%s attempt=%d elapsed_ms=%d progress=%s',
                            code, attempt, int((time.monotonic()-started)*1000), progress)
                # Existing governed DOCX/PPTX draft recovery owns this proposal.
                # Charge the same allowance; its next call still uses this deadline.
                if (allow_parameter_recovery and not self.recovery_used
                        and time.monotonic() < self.deadline
                        and getattr(error, 'details', {}).get('finish_reason') == 'error'):
                    self.recovery_used = True
                    raise
                recoverable = (not self.recovery_used and time.monotonic() < self.deadline and (
                    (code == 'MODEL_OUTPUT_TRUNCATED' and self.truncation_recovery) or
                    (self.no_output_retry and code in {'MODEL_TIMEOUT', 'MODEL_UPSTREAM_UNAVAILABLE'} and not progress)))
                if not recoverable:
                    self.failure = self._error(code, phase='stream' if progress else 'first_output', attempts=attempt)
                    raise self.failure from None
                self.recovery_used = True
                if code == 'MODEL_OUTPUT_TRUNCATED':
                    prefix, original_id, expected_tools = text, response_id, list(tool_calls.values())
                    request = self._recovery_request(request, prefix, expected_tools)
                    recovering = True
                # Transient first-output retry uses exactly the same model context.
            finally:
                if stream is not None and hasattr(stream, 'aclose'):
                    with suppress(Exception):
                        async with asyncio.timeout(2):
                            await stream.aclose()

    def _recovery_request(self, request, prefix, names):
        result = {**request, 'messages': list(request.get('messages') or [])}
        if names:
            allowed = [s for s in request.get('tools') or []
                       if s.get('function', {}).get('name') in names]
            if len({s['function']['name'] for s in allowed}) != len(set(names)):
                self.failure = self._error('MODEL_OUTPUT_TRUNCATED')
                raise self.failure
            result['tools'] = allowed
            result.pop('tool_choice', None)
            instruction = ('上次工具参数因输出长度限制被截断，尚未执行。仅重新提出同一操作的完整参数，'
                '不要拼接残缺JSON，不重复已完成的其他工具步骤；保留事实、来源、完整内容及版式要求。'
                '省略调用前说明，直接提交完整工具调用。无法完整提供时不要声称成功。')
        else:
            result['tools'] = []
            result.pop('tool_choice', None)
            if prefix:
                result['messages'].append(AssistantMsg('assistant', [TextBlock(text=prefix)]))
            instruction = ('上次回答因输出长度限制中断。继续完成紧接在前文之后的剩余回答，不重复前文。'
                '只基于当前已有证据，不调用工具、不虚构新结果；简明完成必要内容，不能省略用户明确要求。')
        result['messages'].append(SystemMsg('system', instruction))
        return result

    def _recovered_final(self, final, prefix, original_id, expected_tools):
        calls = [b for b in final.content if isinstance(b, ToolCallBlock)]
        text = ''.join(b.text for b in final.content if isinstance(b, TextBlock))
        valid = bool(calls) if expected_tools else bool(text.strip()) and not calls
        if expected_tools:
            valid = valid and Counter(b.name for b in calls) == Counter(expected_tools)
            for call in calls:
                try:
                    value = json.loads(call.input) if isinstance(call.input, str) else call.input
                    valid = valid and isinstance(value, dict)
                except (ValueError, TypeError):
                    valid = False
        elif prefix and (text.startswith(prefix) or (len(prefix) >= 16 and prefix[-16:] in text)):
            valid = False
        if not valid:
            raise self._error('MODEL_OUTPUT_TRUNCATED')
        result = copy(final)
        result.id = original_id or final.id
        # The final response is a complete snapshot, not another incremental prefix.
        result.content = ([TextBlock(text=prefix)] if prefix else []) + calls if expected_tools else [TextBlock(text=prefix + text)]
        return result
