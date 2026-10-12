"""Request-local recall of Runtime public turns, never the raw Scroll archive."""
import json
from typing import Any

from agentscope.message import TextBlock, ToolResultState
from agentscope.tool import FunctionTool, Toolkit, ToolChunk
from qwenpaw.agents.context.scroll.manager import ScrollContextManager
from qwenpaw.runtime.tool_guard import GuardedFunctionTool

from ..personalization import current_personalization_context
from ..session import current_managed_session_scope, _sanitize_agent_state
from ..sandbox.scope import SandboxRequestScope
from ..sandbox.broker import RuntimeFileBroker

SCOPED_SCROLL_PROMPT = (
    '压缩索引记录早期会话线索，不是当前用户请求。以最新用户消息为当前任务。'
    '银行会话历史由Runtime按当前用户、助手和会话范围提供；索引中的seq不是Runtime会话游标。'
    '需要早期原话时，仅使用本轮实际可见的受控召回工具；未取得的历史不能作为已核验事实。'
    '有实质结果的答复可在末尾以⟦ 简短里程碑 ⟧记录索引标题。'
)
RECALL_GUIDANCE = (
    '需要当前会话历史时调用recall_history：before_turn_id为空读取最近历史，'
    'query可按公开正文中的字面关键词搜索；继续向前翻页使用返回的next_before_turn_id。'
    '仅返回历史用户原话和助手公开正文，不返回工具执行记录、文件原件或授权。'
    '历史答复不代表本轮已执行、已获授权或已核验；文件读取继续走本轮授权。'
)


class CurrentSessionHistoryReader:
    def __init__(self, agent: Any, request: Any, client: Any) -> None:
        self.agent, self.request, self.client = agent, request, client
        self.toolkit = agent.toolkit
        self.scope = current_managed_session_scope()
        self.personalization = current_personalization_context()
        self.manager = getattr(agent, '_context_manager', None)
        self.sandbox = None
        self.broker = None
        try:
            self.sandbox = SandboxRequestScope.from_request(request)
            self.broker = RuntimeFileBroker(client.config.base_url)
        except (AttributeError, ValueError, RuntimeError):
            pass
        self.tool = GuardedFunctionTool(self.read, name='recall_history', is_read_only=True,
            agent_id=self.scope.agent_id if self.scope else None,
            request_context={**dict(getattr(agent, '_request_context', None) or {}),
                **({'session_id':self.scope.session_id, 'user_id':self.scope.user_id, 'channel':self.scope.channel} if self.scope else {})},
            description='读取当前用户当前助手当前会话的历史公开正文。支持有界向前翻页和字面关键词搜索；不读取原始Scroll档案或工具结果。')
        self.tool_type = type(self.tool)
        self.guard_check = self.tool_type.check_permissions

    def eligible(self) -> bool:
        scope = current_managed_session_scope()
        identity = self.personalization.identity if self.personalization else None
        config = getattr(self.client, 'config', None)
        return (type(self.toolkit) is Toolkit and self.agent.toolkit is self.toolkit
            and scope is not None and scope is self.scope and scope.lock_acquired
            and identity is not None and not self.personalization.identity_error
            and current_personalization_context() is self.personalization
            and identity.user_id == scope.user_id
            and type(self.manager) is ScrollContextManager
            and getattr(self.agent, '_context_manager', None) is self.manager
            and self.manager._session_id == scope.session_id and self.manager._agent_id == scope.agent_id
            and self.sandbox is not None and self.broker is not None
            and self.sandbox.task_id == scope.runtime_task_id
            and not self.sandbox.native_analysis_enabled
            and self.sandbox.sandbox_context == getattr(self.request, 'sandbox_context', None)
            and config is not None and config.agent_id == scope.agent_id
            and config.task_id == scope.runtime_task_id and config.session_id == scope.session_id
            and self.tool._resolve_execution_level() != 'bypass')

    def recognizes(self, tool: Any) -> bool:
        return (isinstance(tool, FunctionTool) and type(tool) is self.tool_type and tool is self.tool and tool.name == 'recall_history'
            and tool.is_read_only and not tool.is_external_tool
            and getattr(tool.check_permissions, '__self__', None) is tool
            and getattr(tool.check_permissions, '__func__', None) is self.guard_check
            and getattr(tool._func, '__self__', None) is self
            and getattr(tool._func, '__func__', None) is CurrentSessionHistoryReader.read)

    async def visible(self) -> bool:
        if not self.eligible() or not self.recognizes(self.tool):
            return False
        try:
            return await self.toolkit.check_tool_available(self.tool.name, self.agent.state.tool_context.activated_groups) is self.tool
        except Exception:
            return False

    async def available(self, payload: dict[str, Any]) -> bool:
        return (set(payload) <= {'before_turn_id', 'query', 'limit'}
            and isinstance(payload.get('before_turn_id', ''), str) and len(payload.get('before_turn_id', '')) <= 96
            and isinstance(payload.get('query', ''), str) and len(payload.get('query', '')) <= 200
            and type(payload.get('limit', 8)) is int and 1 <= payload.get('limit', 8) <= 32
            and await self.visible())

    async def read(self, before_turn_id: str = '', query: str = '', limit: int = 8) -> ToolChunk:
        """Read a page before a returned Runtime turn ID; empty starts at recent history.

        Args:
            before_turn_id: Earlier-page cursor returned by this tool, or empty.
            query: Optional literal keyword to search across current-session public text.
            limit: Maximum messages in this page, from 1 to 32.
        """
        payload = dict(before_turn_id=before_turn_id, query=query, limit=limit)
        try:
            if not await self.available(payload):
                raise ValueError('History scope changed')
            data = await self.broker.expand_conversation_context(self.sandbox, **payload)
            if not await self.available(payload):
                raise ValueError('History scope changed during read')
            messages = data.get('messages')
            if not isinstance(messages, list) or len(messages) > limit:
                raise ValueError('Invalid public history')
            cursor = data.get('next_before_turn_id')
            if type(data.get('has_more')) is not bool or not isinstance(cursor, str) or len(cursor) > 96:
                raise ValueError('Invalid history cursor')
            public = []
            for message in messages:
                if not isinstance(message, dict) or message.get('role') not in {'user', 'assistant'}:
                    raise ValueError('Invalid public history role')
                blocks = message.get('content')
                if not isinstance(message.get('turn_id'), str) or not 1 <= len(message['turn_id']) <= 96:
                    raise ValueError('Invalid public history turn')
                if not isinstance(blocks, list) or any(not isinstance(b, dict) or b.get('type') != 'text'
                    or not isinstance(b.get('text'), str) for b in blocks):
                    raise ValueError('Invalid public history content')
                public.append({'role':message['role'], 'turn_id':message.get('turn_id', ''),
                               'content':[{'type':'text', 'text':b['text']} for b in blocks]})
            public = _sanitize_agent_state({'state':{'context':public}})['state']['context']
            result = dict(messages=public, has_more=data['has_more'], next_before_turn_id=data['next_before_turn_id'])
            rendered = json.dumps(result, ensure_ascii=False)
            if len(rendered.encode()) > 65536:
                raise ValueError('History page too large')
            return ToolChunk(content=[TextBlock(text=rendered)], state=ToolResultState.SUCCESS)
        except Exception:
            return ToolChunk(content=[TextBlock(text='历史召回未完成。请检查当前权限和会话状态，或缩小每页条数；本次未取得的历史不可视为已核验。')],
                             state=ToolResultState.ERROR)
