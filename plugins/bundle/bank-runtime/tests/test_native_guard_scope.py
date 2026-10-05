import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from agentscope.permission import PermissionBehavior, PermissionDecision

PLUGIN_ROOT=Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:sys.path.insert(0,str(PLUGIN_ROOT))

from bank_runtime.gateway.middleware import BankRuntimeGatewayMiddleware, GatewayPermissionEngine
from bank_runtime.sandbox.hooks import BankRuntimeSandboxInstallHook, BankRuntimeSandboxCleanupHook
from bank_runtime.sandbox.native_tools import NATIVE_TOOL_NAMES
from qwenpaw.security.tool_guard.engine import ToolGuardEngine
from qwenpaw.security.tool_guard.guardians.file_guardian import FilePathToolGuardian
from qwenpaw.security.tool_guard.guardians.rule_guardian import RuleBasedToolGuardian, SharedSafetyToolGuardian
from qwenpaw.security.tool_guard.guardians.shell_evasion_guardian import ShellEvasionGuardian
from qwenpaw.security.tool_guard.virtual_paths import ContainerGuardPaths, container_path_view, current_container_paths


def test_native_default_secret_directories_use_container_home():
    guardian = FilePathToolGuardian()
    guardian._enabled = True
    with container_path_view(ContainerGuardPaths(task_id='task')):
        for name in ('.qwenpaw.secret', '.copaw.secret'):
            assert guardian.guard('read_file', {'file_path': '~/' + name + '/key'})


def test_native_relative_admin_rule_stays_at_project_root_when_cwd_changes():
    guardian = FilePathToolGuardian(sensitive_files=['private.txt', '/workspace/input/secret.txt'])
    guardian._enabled = True
    with container_path_view(ContainerGuardPaths(task_id='task', cwd='/workspace/output')):
        assert guardian.guard('read_file', {'file_path': '../private.txt'})
        assert guardian.guard('read_file', {'file_path': '/workspace/input/secret.txt'})


def request_context(task='task', user='user'):
    envelope={'task_id':task,'task_scope_id':'scope-'+task,'sandbox_instance_id':'box-'+task,
        'context_manifest_id':'manifest','signature':'signed','native_analysis_enabled':True,
        'isolation_level':'container','expires_at':'2099-01-01T00:00:00Z'}
    middleware=BankRuntimeGatewayMiddleware(SimpleNamespace(config=SimpleNamespace(agent_id='agent')),
        sandbox_executor=SimpleNamespace(sandbox_context=envelope),native_analysis_enabled=True)
    request=SimpleNamespace(channel='bank-runtime',runtime_task_id=task,user_id=user,session_id=task,
        sandbox_context=envelope,attachments_manifest=[],
        runtime_tool_gateway={'base_url':'http://runtime','capability_snapshot_hash':'a'*64},
        runtime_tool_visibility={'worker_type':'qwenpaw','binding_snapshot_hash':'sha256:'+'a'*64,
            'authoritative':True,'worker_tool_names':list(NATIVE_TOOL_NAMES)})
    agent=SimpleNamespace(toolkit=SimpleNamespace(tool_groups=[SimpleNamespace(tools=[])]),
        _acting_middlewares=[middleware],_engine=GatewayPermissionEngine(SimpleNamespace(),middleware),_system_prompt='')
    return SimpleNamespace(request=request,agent=agent,agent_id='agent',extras={})


@pytest.fixture
def real_guard(monkeypatch):
    import qwenpaw.config.config as config
    monkeypatch.setenv('QWENPAW_SERVICE_TOKEN','test-service-token')
    monkeypatch.setattr(config,'load_agent_config',lambda _:SimpleNamespace(approval_level='auto'))
    files=FilePathToolGuardian(sensitive_files=['/workspace/scratch/private.txt'])
    files._enabled=True
    engine=ToolGuardEngine(guardians=[SharedSafetyToolGuardian(),files,RuleBasedToolGuardian(),ShellEvasionGuardian()],enabled=True)
    engine._denied_tools=set()
    monkeypatch.setattr('qwenpaw.security.tool_guard.engine.get_guard_engine',lambda:engine)
    approval=AsyncMock(return_value=PermissionDecision(behavior=PermissionBehavior.DENY,message='user denied'))
    monkeypatch.setattr('qwenpaw.runtime.tool_guard._ask_user_approval',approval)
    return engine,approval


@pytest.mark.asyncio
async def test_verified_native_install_uses_real_guard_container_relative_sensitive_path(real_guard):
    ctx=request_context()
    try:
        await BankRuntimeSandboxInstallHook().run(ctx)
        tool=next(item for item in ctx.agent.toolkit.tool_groups[0].tools if item.name=='read_file')
        decision=await tool.check_permissions({'file_path':'private.txt'})
        assert decision.behavior==PermissionBehavior.DENY
        assert real_guard[1].await_count==1
    finally:
        await BankRuntimeSandboxCleanupHook().run(ctx)


@pytest.mark.asyncio
async def test_native_guard_model_paths_never_resolve_on_host(real_guard,monkeypatch):
    ctx=request_context()
    try:
        await BankRuntimeSandboxInstallHook().run(ctx)
        calls=[]
        original=real_guard[0].guard
        def capture(*args,**kwargs):
            result=original(*args,**kwargs);calls.append(result);return result
        monkeypatch.setattr(real_guard[0],'guard',capture)
        tool=next(item for item in ctx.agent.toolkit.tool_groups[0].tools if item.name=='execute_shell_command')
        with monkeypatch.context() as model_paths:
            model_paths.setattr(Path,'resolve',lambda *args,**kwargs:(_ for _ in ()).throw(AssertionError('host filesystem resolution')))
            await tool.check_permissions({'command':'python /workspace/scratch/analysis.py','cwd':'/workspace/output'})
            assert not calls[0].guardians_failed
            assert len(calls[0].guardians_used)==4
    finally:
        await BankRuntimeSandboxCleanupHook().run(ctx)


@pytest.mark.asyncio
@pytest.mark.parametrize('level', ['auto', 'smart', 'strict'])
async def test_native_retains_denied_list_and_approval_levels(real_guard, monkeypatch, level):
    import qwenpaw.config.config as config
    monkeypatch.setattr(config, 'load_agent_config', lambda _:SimpleNamespace(approval_level=level))
    ctx=request_context()
    try:
        await BankRuntimeSandboxInstallHook().run(ctx)
        tool=next(item for item in ctx.agent.toolkit.tool_groups[0].tools if item.name=='read_file')
        real_guard[0]._denied_tools={'read_file'}
        decision=await tool.check_permissions({'file_path':'ordinary.txt'})
        assert decision.behavior==PermissionBehavior.DENY
        assert real_guard[1].await_count==0
        real_guard[0]._denied_tools=set()
        decision=await tool.check_permissions({'file_path':'private.txt'})
        assert decision.behavior==PermissionBehavior.DENY
        assert real_guard[1].await_count==1
    finally:
        await BankRuntimeSandboxCleanupHook().run(ctx)
    assert current_container_paths() is None
    assert (await tool.check_permissions({'file_path':'ordinary.txt'})).behavior==PermissionBehavior.DENY


@pytest.mark.asyncio
async def test_native_concurrent_real_guard_scopes_reach_threads_and_reset(real_guard,monkeypatch):
    from threading import Barrier
    barrier=Barrier(2, timeout=5)
    captured=[]
    original=real_guard[0].guard
    def guard(*args, **kwargs):
        paths=current_container_paths()
        barrier.wait()
        captured.append((paths.task_id, paths.cwd))
        return original(*args, **kwargs)
    monkeypatch.setattr(real_guard[0], 'guard', guard)
    async def run(task, cwd):
        ctx=request_context(task)
        try:
            await BankRuntimeSandboxInstallHook().run(ctx)
            tool=next(item for item in ctx.agent.toolkit.tool_groups[0].tools if item.name=='execute_shell_command')
            await tool.check_permissions({'command':'echo ordinary', 'cwd':cwd})
            assert current_container_paths() is None
        finally:
            await BankRuntimeSandboxCleanupHook().run(ctx)
    await asyncio.gather(run('first','/workspace/scratch'),run('second','/workspace/output'))
    assert sorted(captured)==[('first','/workspace/scratch'),('second','/workspace/output')]
    assert current_container_paths() is None


@pytest.mark.asyncio
async def test_native_guard_context_resets_when_permission_check_raises(real_guard,monkeypatch):
    ctx=request_context()
    try:
        await BankRuntimeSandboxInstallHook().run(ctx)
        tool=next(item for item in ctx.agent.toolkit.tool_groups[0].tools if item.name=='read_file')
        def explode(*args, **kwargs):
            assert current_container_paths().task_id=='task'
            raise RuntimeError('guard failed')
        monkeypatch.setattr(real_guard[0], 'guard', explode)
        with pytest.raises(RuntimeError, match='guard failed'):
            await tool.check_permissions({'file_path':'ordinary.txt'})
        assert current_container_paths() is None
    finally:
        await BankRuntimeSandboxCleanupHook().run(ctx)


@pytest.mark.asyncio
async def test_native_file_namespace_and_shell_cwd_match_actual_contract(real_guard,monkeypatch):
    files=next(item for item in real_guard[0]._guardians if isinstance(item,FilePathToolGuardian))
    files.add_sensitive_file('/workspace/output/private.txt')
    ctx=request_context()
    seen=[]
    original=real_guard[0].guard
    def capture(name, arguments, **kwargs):
        seen.append((name, dict(arguments), current_container_paths().cwd))
        return original(name, arguments, **kwargs)
    monkeypatch.setattr(real_guard[0], 'guard', capture)
    try:
        await BankRuntimeSandboxInstallHook().run(ctx)
        tools={item.name:item for item in ctx.agent.toolkit.tool_groups[0].tools}
        arguments={'file_path':'output/private.txt'}
        denied=await tools['read_file'].check_permissions(arguments)
        assert denied.behavior==PermissionBehavior.DENY
        assert arguments=={'file_path':'output/private.txt'}
        arguments={'command':'cat ./private.txt', 'cwd':'output'}
        denied=await tools['execute_shell_command'].check_permissions(arguments)
        assert denied.behavior==PermissionBehavior.DENY
        assert arguments=={'command':'cat ./private.txt', 'cwd':'output'}
        assert seen[-1][2]=='/workspace/output'
    finally:
        await BankRuntimeSandboxCleanupHook().run(ctx)
