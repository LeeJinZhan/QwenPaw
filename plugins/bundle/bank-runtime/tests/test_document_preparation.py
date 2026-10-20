"""Follow-up preparation must use current-task search/select and independent permits."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest
from agentscope.message import TextBlock, ToolResultState
from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.tool import FunctionTool, ToolChunk
from bank_runtime.gateway.middleware import BankRuntimeGatewayMiddleware, GatewayPermissionEngine
from bank_runtime.sandbox import file_refs, tools
from bank_runtime.gateway import document_inputs
from bank_runtime.sandbox.cache import PreparedSandboxFile
from bank_runtime.sandbox.scope import SandboxRequestScope


class Client:
    config = SimpleNamespace(task_id="task_followup")
    def __init__(self, denied=""):
        self.calls, self.results, self.guards = [], [], []
        self.denied = denied

    async def preflight(self, name, payload, call_id):
        self.calls.append((name, payload))
        if name == self.denied:
            raise RuntimeError("policy denied")
        return {"tool_call_id": call_id}

    async def report_guard(self, preflight, decision):
        self.guards.append(decision)

    async def report_result(self, *args):
        self.results.append(args)


class Delegate:
    async def check_permission(self, *args):
        return PermissionDecision(behavior=PermissionBehavior.ALLOW, message="allowed")


@pytest.fixture
def followup(tmp_path, monkeypatch):
    registry = file_refs.FileRefRegistry(tmp_path)
    monkeypatch.setattr(file_refs, "get_file_ref_registry", lambda: registry)
    monkeypatch.setattr(tools, "get_file_ref_registry", lambda: registry)
    monkeypatch.setattr(document_inputs, "get_file_ref_registry", lambda: registry)
    record = {"file_id": "file_old", "display_name": "流水.xlsx", "source": "conversation", "readable": True}
    scope = SandboxRequestScope("task_followup", {"task_id": "task_followup", "expires_at": "2099-01-01T00:00:00+00:00"}, (), ())
    scope.historical_files = {"file_old": record}
    class Broker:
        async def search(self, *args, **kwargs):
            return [record]
    class Cache:
        calls = 0
        async def prepare_files(self, scope, ids, broker, selection_records=None):
            assert selection_records == [{"file_id": "file_old", "source": "conversation", "selection_mode": "model_metadata_selection"}]
            self.calls += 1
            root = tmp_path / scope.task_id
            root.mkdir(exist_ok=True)
            path = root / "original.xlsx"
            path.write_bytes(b"test source")
            return [PreparedSandboxFile(ids[0], path, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", path.stat().st_size, "流水.xlsx", "2099-01-01T00:00:00+00:00", task_id=scope.task_id)]
    class Processor:
        read_failures = {}
        def process(self, files, file_refs):
            return [TextBlock(text=json.dumps({"file_id": files[0].file_id, "file_ref": file_refs[files[0].file_id]}))]
    state = tools.SandboxToolState(scope, Broker(), Cache(), Processor())
    token = tools.set_sandbox_tool_state(state)
    # Return chunks to exercise the SDK path without its legacy ToolResponse repr bug.
    async def search(**kwargs):
        response = await tools.runtime_sandbox_files_search(**kwargs)
        return ToolChunk(content=response.content, state=response.state)
    async def select(file_ids: list[str]):
        response = await tools.runtime_sandbox_files_select(file_ids)
        return ToolChunk(content=response.content, state=response.state)
    search.__name__ = "runtime_sandbox_files_search"
    select.__name__ = "runtime_sandbox_files_select"
    registered = [FunctionTool(search), FunctionTool(select)]
    client = Client()
    middleware = BankRuntimeGatewayMiddleware(client)
    agent = SimpleNamespace(toolkit=SimpleNamespace(tool_groups=[SimpleNamespace(tools=registered)]))
    middleware.native_skills = SimpleNamespace(agent=agent, recognizes=lambda tool: False)
    middleware.allowed_tool_names = frozenset(tool.name for tool in registered) | {"MinerU__parse_documents"}
    engine = agent._engine = GatewayPermissionEngine(Delegate(), middleware)
    try:
        yield SimpleNamespace(state=state, client=client, middleware=middleware, engine=engine, registry=registry, agent=agent)
    finally:
        registry.revoke_task(scope.task_id)
        tools.reset_sandbox_tool_state(token)


@pytest.mark.asyncio
async def test_followup_parse_prepares_exact_history_file_before_parse_permit(followup):
    payload = {"documents": [{"file_id": "file_old"}]}
    decision = await followup.engine.check_permission(SimpleNamespace(name="MinerU__parse_documents"), payload)
    assert decision.behavior == PermissionBehavior.ALLOW
    assert [name for name, _ in followup.client.calls] == ["runtime_sandbox_files_search", "runtime_sandbox_files_select", "MinerU__parse_documents"]
    bound = followup.client.calls[-1][1]["documents"][0]
    assert followup.registry.resolve(bound["file_ref"], expected_task_id="task_followup").file_id == "file_old"
    assert [entry[1] for entry in followup.client.results] == ["completed", "completed"]
    assert followup.state.cache.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('denied', ['', 'runtime_sandbox_files_search', 'runtime_sandbox_files_select'])
async def test_native_direct_history_selection_discovers_exact_candidate_with_normal_guards(followup, monkeypatch, denied):
    followup.state.scope.sandbox_context.update(native_analysis_enabled=True, isolation_level='container')
    followup.middleware.native_analysis_enabled = True
    followup.client.denied = denied
    async def native_blocks(state, prepared):
        state.scope.prepared_originals['file_old'] = {'container_path': '/workspace/input/file_old/original.xlsx'}
        return []
    monkeypatch.setattr(tools, '_prepared_blocks', native_blocks)
    if denied:
        with pytest.raises(Exception):
            await followup.middleware._run_file_preparation_tool('runtime_sandbox_files_select', {'file_ids': ['file_old']})
        assert followup.state.cache.calls == 0
    else:
        await followup.middleware._run_file_preparation_tool('runtime_sandbox_files_select', {'file_ids': ['file_old']})
        assert [name for name, _ in followup.client.calls] == ['runtime_sandbox_files_search', 'runtime_sandbox_files_select']
        assert followup.state.cache.calls == 1
        assert followup.state.scope.selected_file_ids == {'file_old'}
        assert followup.client.guards == ['allow', 'allow']


@pytest.mark.asyncio
async def test_native_direct_selection_never_substitutes_same_named_new_file(followup):
    followup.state.scope.sandbox_context.update(native_analysis_enabled=True, isolation_level='container')
    followup.middleware.native_analysis_enabled = True
    async def newer(*args, **kwargs):
        return [{'file_id': 'file_newer', 'display_name': '流水.xlsx', 'source': 'conversation', 'readable': True}]
    followup.state.broker.search = newer
    with pytest.raises(Exception):
        await followup.middleware._run_file_preparation_tool('runtime_sandbox_files_select', {'file_ids': ['file_old']})
    assert followup.state.cache.calls == 0
    assert not followup.state.scope.selected_file_ids


@pytest.mark.asyncio
async def test_followup_select_denial_does_not_prepare_or_parse(followup):
    followup.client.denied = "runtime_sandbox_files_select"
    decision = await followup.engine.check_permission(SimpleNamespace(name="MinerU__parse_documents"), {"documents": [{"file_id": "file_old"}]})
    assert decision.behavior == PermissionBehavior.DENY
    assert [name for name, _ in followup.client.calls] == ["runtime_sandbox_files_search", "runtime_sandbox_files_select"]
    assert followup.state.cache.calls == 0


@pytest.mark.asyncio
async def test_followup_does_not_select_newer_file_with_same_name(followup):
    async def search(*args, **kwargs):
        return [{"file_id": "file_newer", "display_name": "流水.xlsx", "source": "conversation", "readable": True}]
    followup.state.broker.search = search
    decision = await followup.engine.check_permission(SimpleNamespace(name="MinerU__parse_documents"), {"documents": [{"file_id": "file_old"}]})
    assert decision.behavior == PermissionBehavior.DENY
    assert followup.state.cache.calls == 0
    assert all(name != "MinerU__parse_documents" for name, _ in followup.client.calls)


@pytest.mark.asyncio
async def test_unbound_selection_tool_cannot_be_used_for_auto_preparation(followup):
    followup.middleware.allowed_tool_names = frozenset({"runtime_sandbox_files_search", "MinerU__parse_documents"})
    decision = await followup.engine.check_permission(SimpleNamespace(name="MinerU__parse_documents"), {"documents": [{"file_id": "file_old"}]})
    assert decision.behavior == PermissionBehavior.DENY
    assert followup.state.cache.calls == 0


@pytest.mark.asyncio
async def test_unknown_placeholder_does_not_infer_history_singleton(followup):
    decision = await followup.engine.check_permission(SimpleNamespace(name="MinerU__parse_documents"), {"documents": [{"file_id": "[runtime-reference-redacted]"}]})
    assert decision.behavior == PermissionBehavior.DENY
    assert followup.client.calls == []


@pytest.mark.asyncio
async def test_concurrent_preparation_uses_one_authorized_copy(followup):
    import asyncio
    original = followup.state.broker.search
    async def slow_search(*args, **kwargs):
        await asyncio.sleep(0.01)
        return await original(*args, **kwargs)
    followup.state.broker.search = slow_search
    decisions = await asyncio.gather(*(followup.engine.check_permission(
        SimpleNamespace(name="MinerU__parse_documents"), {"documents": [{"file_id": "file_old"}]}) for _ in range(2)))
    assert all(item.behavior == PermissionBehavior.ALLOW for item in decisions)
    assert followup.state.cache.calls == 1


@pytest.mark.asyncio
async def test_original_tool_guard_denial_stops_preparation(followup):
    async def deny(tool, payload):
        return PermissionDecision(behavior=PermissionBehavior.DENY, message="policy denied")
    followup.engine.delegate.check_permission = deny
    decision = await followup.engine.check_permission(SimpleNamespace(name="MinerU__parse_documents"), {"documents": [{"file_id": "file_old"}]})
    assert decision.behavior == PermissionBehavior.DENY
    assert followup.state.cache.calls == 0
    assert "block" in followup.client.guards


@pytest.mark.asyncio
async def test_mismatched_task_state_is_rejected_before_preparation(followup):
    followup.client.config = SimpleNamespace(task_id="task_other")
    decision = await followup.engine.check_permission(SimpleNamespace(name="MinerU__parse_documents"), {"documents": [{"file_id": "file_old"}]})
    assert decision.behavior == PermissionBehavior.DENY
    assert followup.client.calls == []


@pytest.mark.asyncio
async def test_failed_preparation_is_not_automatically_repeated(followup):
    async def fail(*args, **kwargs):
        raise RuntimeError("network failed")
    followup.state.broker.search = fail
    for _ in range(2):
        result = await followup.engine.check_permission(SimpleNamespace(name="MinerU__parse_documents"), {"documents": [{"file_id": "file_old"}]})
        assert result.behavior == PermissionBehavior.DENY
    assert [name for name, _ in followup.client.calls] == ["runtime_sandbox_files_search"]


@pytest.mark.asyncio
@pytest.mark.parametrize("filename", ["流" * 245 + ".xlsx", "流水\n银行.xlsx"])
async def test_history_lookup_uses_valid_bounded_query_and_exact_file_id(followup, filename):
    followup.state.scope.historical_files["file_old"]["display_name"] = filename
    seen = []
    original = followup.state.broker.search
    async def search(*args, **kwargs):
        seen.append(kwargs["query"])
        return await original(*args, **kwargs)
    followup.state.broker.search = search
    decision = await followup.engine.check_permission(SimpleNamespace(name="MinerU__parse_documents"), {"documents": [{"file_id": "file_old"}]})
    assert decision.behavior == PermissionBehavior.ALLOW
    assert seen and len(seen[0]) <= 200 and all(ord(char) >= 32 for char in seen[0])
    assert filename.startswith(seen[0])
    assert followup.state.cache.calls == 1


@pytest.mark.asyncio
async def test_duplicate_source_does_not_replace_explicit_old_capability(followup):
    payload = {"documents": [{"file_id": "file_old"}, {"file_id": "file_old", "file_ref": "fr1_explicit_old"}]}
    prepared = await followup.middleware.document_preparation.prepare("MinerU__parse_documents", payload, expected_task_id="task_followup")
    assert prepared["documents"][0]["file_ref"] != "fr1_explicit_old"
    assert prepared["documents"][1]["file_ref"] == "fr1_explicit_old"


def register_parser(followup):
    async def parse(documents: list[dict], options: dict | None = None):
        source = documents[0]
        assert followup.registry.resolve(source['file_ref'], expected_task_id='task_followup').file_id == 'file_old'
        return ToolChunk(state=ToolResultState.SUCCESS, content=[TextBlock(text=json.dumps({'items': [{
            'file_id': 'file_old', 'document_ref': 'ds1_current', 'status': 'completed',
            'content_mode': 'structured', 'inventory': {'engine': 'table-facts-3',
                'sheets': [{'name': '流水', 'rows': 100000, 'columns': []}]}}]}))])
    parse.__name__ = 'MinerU__parse_documents'
    followup.agent.toolkit.tool_groups[0].tools.append(FunctionTool(parse))
    followup.middleware.allowed_tool_names |= {'MinerU__read_range', 'MinerU__aggregate', 'MinerU__search', 'MinerU__analyze'}


@pytest.mark.asyncio
async def test_query_first_prepares_and_parses_before_final_query_permit(followup):
    register_parser(followup)
    args = {'document_ref': 'file_old', 'sheet': '流水', 'rows': [50001, 50001], 'format': 'source'}
    decision = await followup.engine.check_permission(SimpleNamespace(name='MinerU__read_range'), args)
    assert decision.behavior == PermissionBehavior.ALLOW
    assert [name for name, _ in followup.client.calls] == ['runtime_sandbox_files_search',
        'runtime_sandbox_files_select', 'MinerU__parse_documents', 'MinerU__read_range']
    assert followup.client.calls[-1][1] == {**args, 'document_ref': 'ds1_current'}
    assert followup.middleware.document_input('MinerU__read_range', args) == followup.client.calls[-1][1]
    assert not followup.middleware.document_reads.failures


@pytest.mark.asyncio
async def test_parallel_query_first_parses_once_and_binds_each_source_alias(followup):
    import asyncio
    register_parser(followup)
    args = [{'document_ref': 'file_old', 'rows': [row, row], 'format': 'source'} for row in (2, 50001, 100001)]
    decisions = await asyncio.gather(*(followup.engine.check_permission(SimpleNamespace(name='MinerU__read_range'), arg) for arg in args))
    assert all(result.behavior == PermissionBehavior.ALLOW for result in decisions)
    assert followup.state.cache.calls == 1
    assert sum(name == 'MinerU__parse_documents' for name, _ in followup.client.calls) == 1
    ref = followup.registry.reference_for_file('file_old', expected_task_id='task_followup')
    decision = await followup.engine.check_permission(SimpleNamespace(name='MinerU__read_range'), {**args[0], 'document_ref': ref})
    assert decision.behavior == PermissionBehavior.ALLOW
    assert followup.client.calls[-1][1]['document_ref'] == 'ds1_current'


@pytest.mark.asyncio
@pytest.mark.parametrize('denied', ['runtime_sandbox_files_select', 'MinerU__parse_documents'])
async def test_query_preparation_denial_stops_query(followup, denied):
    register_parser(followup)
    followup.client.denied = denied
    decision = await followup.engine.check_permission(SimpleNamespace(name='MinerU__read_range'), {'document_ref': 'file_old'})
    assert decision.behavior == PermissionBehavior.DENY
    assert all(name != 'MinerU__read_range' for name, _ in followup.client.calls)
    assert followup.middleware.document_reads.pending
    assert 'FILE_ACCESS_DENIED' in followup.middleware.document_reads.failures.values()


@pytest.mark.asyncio
async def test_unbound_parser_cannot_be_invoked_by_query(followup):
    register_parser(followup)
    followup.middleware.allowed_tool_names -= {'MinerU__parse_documents'}
    decision = await followup.engine.check_permission(SimpleNamespace(name='MinerU__read_range'), {'document_ref': 'file_old'})
    assert decision.behavior == PermissionBehavior.DENY
    assert all(name != 'MinerU__read_range' for name, _ in followup.client.calls)


@pytest.mark.asyncio
async def test_current_file_ref_query_first_resolves_exact_file_without_history_search(followup):
    register_parser(followup)
    await followup.middleware._run_file_preparation_tool('runtime_sandbox_files_search', {'query': '流水.xlsx'})
    await followup.middleware._run_file_preparation_tool('runtime_sandbox_files_select', {'file_ids': ['file_old']})
    ref = followup.registry.reference_for_file('file_old', expected_task_id='task_followup')
    followup.state.scope.historical_files.clear()
    before = len(followup.client.calls)
    decision = await followup.engine.check_permission(SimpleNamespace(name='MinerU__read_range'), {'document_ref': ref})
    assert decision.behavior == PermissionBehavior.ALLOW
    assert [name for name, _ in followup.client.calls[before:]] == ['MinerU__parse_documents', 'MinerU__read_range']
    assert followup.client.calls[-1][1]['document_ref'] == 'ds1_current'


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['expired', 'other_task', 'tampered'])
async def test_explicit_file_capability_cannot_be_renewed_or_promoted_by_query(followup, kind, tmp_path):
    from datetime import datetime, timezone, timedelta
    register_parser(followup)
    await followup.middleware._run_file_preparation_tool('runtime_sandbox_files_search', {'query': '流水.xlsx'})
    await followup.middleware._run_file_preparation_tool('runtime_sandbox_files_select', {'file_ids': ['file_old']})
    ref = followup.registry.reference_for_file('file_old', expected_task_id='task_followup')
    if kind == 'expired':
        followup.registry.revoke_task('task_followup')
    elif kind == 'other_task':
        root = tmp_path / 'task_other'; root.mkdir(); path = root / 'source.xlsx'; path.write_bytes(b'other')
        ref = followup.registry.issue(PreparedSandboxFile('file_old', path, 'application/octet-stream', 5,
            'source.xlsx', '', task_id='task_other'), expires_at=datetime.now(timezone.utc) + timedelta(minutes=5))
    else:
        ref = ref[:-1] + ('0' if ref[-1] != '0' else '1')
    before = len(followup.client.calls)
    decision = await followup.engine.check_permission(SimpleNamespace(name='MinerU__read_range'), {'document_ref': ref})
    assert decision.behavior == PermissionBehavior.DENY
    # Denied capability gets an auditable query preflight + blocked Guard;
    # it must never trigger a new search/select/parse or an execution permit.
    assert [name for name, _ in followup.client.calls[before:]] == ['MinerU__read_range']
    assert followup.client.calls[-1][1]['document_ref'] == ref
    assert followup.client.guards[-1] == 'block'
    assert followup.middleware.document_reads.pending


@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['MinerU__read_range', 'MinerU__search', 'MinerU__analyze'])
async def test_query_guard_denial_is_retained_after_successful_preparation(followup, name):
    register_parser(followup)
    async def guard(tool, payload):
        return PermissionDecision(behavior=PermissionBehavior.DENY if tool.name == name else PermissionBehavior.ALLOW, message='policy')
    followup.engine.delegate.check_permission = guard
    decision = await followup.engine.check_permission(SimpleNamespace(name=name), {'document_ref': 'file_old'})
    assert decision.behavior == PermissionBehavior.DENY
    assert followup.middleware.document_reads.pending
    assert 'FILE_ACCESS_DENIED' in followup.middleware.document_reads.failures.values()
