"""On-demand exact-source discovery/preparation through registered file tools."""
from copy import deepcopy
import asyncio
import re

from ..sandbox.file_refs import FileRefError
from ..sandbox import file_refs
from ..sandbox.tools import current_sandbox_tool_state
from ..sandbox.scope import MAX_TASK_FILES
from .client import GatewayError
from .document_access import DOCUMENT_TOOLS
from .document_inputs import normalize_document_input


class DocumentSourcePreparation:
    def __init__(self, run_tool):
        self.run_tool = run_tool
        self.attempted = set()
        self.lock = asyncio.Lock()
        self.query_lock = asyncio.Lock()
        self.selection_lock = asyncio.Lock()
        self.discovery_lock = asyncio.Lock()
        self.discovery_attempted = set()
        self.parse_attempted = set()

    async def prepare(self, name, arguments, *, expected_task_id, ledger=None):
        raw = name.removeprefix('MinerU__')
        selection = name == 'runtime_sandbox_files_select'
        if not selection and (name != 'MinerU__' + raw or raw not in DOCUMENT_TOOLS):
            return arguments
        state = current_sandbox_tool_state()
        if state is None:
            return arguments
        if not expected_task_id or state.scope.task_id != expected_task_id:
            raise GatewayError("文件准备任务范围不一致。", code="FILE_ACCESS_DENIED")
        if selection:
            if state.scope.native_analysis_enabled:
                async with self.selection_lock:
                    ids = arguments.get('file_ids')
                    if isinstance(ids, list) and 0 < len(ids) <= MAX_TASK_FILES:
                        for file_id in ids:
                            if isinstance(file_id, str) and file_id in state.scope.historical_files:
                                await self._discover(state, file_id)
            return arguments
        if raw == 'parse_documents':
            async with self.lock:
                return await self._prepare(state, arguments)
        if ledger is None:
            return arguments
        # The parse permission path acquires the source lock itself. A separate
        # query lock deduplicates parsing without recursively acquiring it.
        async with self.query_lock:
            return await self._prepare_query(state, name, arguments, ledger)

    async def _discover(self, state, file_id):
        async with self.discovery_lock:
            await self._discover_locked(state, file_id)

    async def _discover_locked(self, state, file_id):
        if file_id in state.scope.discovered_files or file_id in state.scope.current_attachment_ids:
            return
        if file_id in self.discovery_attempted:
            raise GatewayError('目标文件当前不可选择，请确认文件版本。', code='FILE_ACCESS_DENIED')
        self.discovery_attempted.add(file_id)
        display_name = state.scope.historical_files.get(file_id, {}).get('display_name')
        if not display_name:
            raise GatewayError('请先查找并确认目标文件。', code='DOCUMENT_ARGUMENT_INVALID')
        # The literal name only finds candidates. Exact ID and current Runtime
        # authorization still decide access; never substitute a same-named file.
        query = re.split(r'[\x00-\x1f]', display_name, maxsplit=1)[0][:200].strip()
        await self.run_tool('runtime_sandbox_files_search', {'query': query,
            'sources': ['conversation', 'assistant_workspace'], 'limit': 50})
        if file_id not in state.scope.discovered_files:
            raise GatewayError('目标文件当前不可选择，请确认文件版本。', code='FILE_ACCESS_DENIED')

    async def _prepare_query(self, state, name, arguments, ledger):
        ref = arguments.get('document_ref')
        if not isinstance(ref, str) or not ref or ref in ledger.documents:
            return arguments
        registry = file_refs.get_file_ref_registry()
        if ref.startswith('fr1_'):
            # Resolve the explicit capability BEFORE aliases or history. Never
            # renew an expired, foreign or tampered capability through file_id.
            file_id = registry.resolve(ref, expected_task_id=state.scope.task_id).file_id
            source = {'file_id': file_id, 'file_ref': ref}
        else:
            file_id = ref
            known = (file_id in state.scope.current_attachment_ids
                     or file_id in state.scope.discovered_files
                     or file_id in state.scope.historical_files)
            if not known:
                return arguments
            source = {'file_id': file_id}
        normalized = normalize_document_input(name, arguments, task_id=state.scope.task_id, ledger=ledger)
        if normalized.get('document_ref') in ledger.documents:
            return normalized
        if file_id in self.parse_attempted:
            raise GatewayError('文件解析尚未确认，不能重复启动。', code='DOCUMENT_READ_INCOMPLETE')
        self.parse_attempted.add(file_id)
        payload = {'documents': [source]}
        await self.run_tool('MinerU__parse_documents', payload)
        normalized = normalize_document_input(name, arguments, task_id=state.scope.task_id, ledger=ledger)
        if normalized.get('document_ref') not in ledger.documents:
            raise GatewayError('文件解析结果尚未确认，不能读取。', code='DOCUMENT_READ_INCOMPLETE')
        return normalized

    async def _prepare(self, state, arguments):
        documents = arguments.get("documents")
        if not isinstance(documents, list) or not documents:
            raise GatewayError("解析需要明确的文件身份。", code="DOCUMENT_ARGUMENT_INVALID")
        value = deepcopy(arguments)
        registry = file_refs.get_file_ref_registry()
        missing = []
        for item in value["documents"]:
            if not isinstance(item, dict) or not isinstance(item.get("file_id"), str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", item["file_id"]):
                raise GatewayError("解析需要有效的文件身份。", code="DOCUMENT_ARGUMENT_INVALID")
            file_id = item["file_id"]
            if item.get("file_ref") not in (None, "", file_id):
                # An explicit capability is never silently replaced or promoted.
                continue
            try:
                item["file_ref"] = registry.reference_for_file(file_id, expected_task_id=state.scope.task_id)
            except FileRefError as exc:
                if file_id in state.scope.current_attachment_ids or exc.code != "FILE_ACCESS_DENIED":
                    raise
                if file_id not in state.scope.discovered_files and file_id not in state.scope.historical_files:
                    raise GatewayError("请先查找并确认目标文件。", code="DOCUMENT_ARGUMENT_INVALID")
                missing.append(file_id)
        for file_id in dict.fromkeys(missing):
            if file_id in self.attempted:
                raise GatewayError("该文件准备尚未成功，请核对当前文件状态。", code="FILE_ACCESS_DENIED")
            self.attempted.add(file_id)
            if file_id not in state.scope.discovered_files:
                await self._discover(state, file_id)
            if file_id not in state.scope.discovered_files:
                raise GatewayError("目标文件当前不可选择，请确认文件版本。", code="FILE_ACCESS_DENIED")
            await self.run_tool("runtime_sandbox_files_select", {"file_ids": [file_id]})
        for item in value["documents"]:
            if (item["file_id"] in missing
                    and item.get("file_ref") in (None, "", item["file_id"])):
                item["file_ref"] = registry.reference_for_file(item["file_id"], expected_task_id=state.scope.task_id)
        return value
