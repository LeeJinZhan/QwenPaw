"""Request-local schema routing; presentation never grants execution authority."""
from types import SimpleNamespace
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bank_runtime.gateway.middleware import BankRuntimeGatewayMiddleware
from bank_runtime.sandbox.scope import SandboxRequestScope
from bank_runtime.sandbox.tools import SandboxToolState, set_sandbox_tool_state, reset_sandbox_tool_state


SCHEMAS = [{"type": "function", "function": {"name": name}} for name in (
    "execute_shell_command", "MinerU__parse_documents", "MinerU__read_range",
    "MinerU__aggregate", "MinerU__read_document_chunks", "MinerU__search", "MinerU__analyze",
    "runtime_sandbox_files_search", "runtime_sandbox_files_select", "knowledge_search", "artifact_generate",
)]


@pytest.mark.asyncio
@pytest.mark.parametrize("extensions,native,shell,expected_legacy", [
    ([".xlsx"], True, True, False), ([".csv", ".xls"], True, True, False),
    ([".xlsx", ".pdf"], True, True, True), ([".xlsx", ""], True, True, True),
    ([".xlsb"], True, True, True), ([".xlsx"], False, True, True),
    ([".xlsx"], True, False, True), ([], True, True, True),
])
async def test_native_schema_routes_only_fully_prepared_supported_tables(extensions, native, shell, expected_legacy):
    scope = SandboxRequestScope("task", {"native_analysis_enabled": native, "isolation_level": "container",
        "analysis_environment": {"packages": {"openpyxl": "3.1.5", "xlrd": "2.0.2"}}},
                                (), tuple(f"f{i}" for i in range(len(extensions))))
    scope.prepared_originals = {f"f{i}": {"container_path": f"/workspace/input/f{i}/{'a'*64}{ext}"}
                               for i, ext in enumerate(extensions)}
    token = set_sandbox_tool_state(SandboxToolState(scope, None, None, SimpleNamespace(read_failures={})))
    try:
        middleware = BankRuntimeGatewayMiddleware(None, native_analysis_enabled=native,
            sandbox_executor=SimpleNamespace(sandbox_context=scope.sandbox_context))
        middleware.allowed_tool_names = frozenset(s["function"]["name"] for s in SCHEMAS
                                                   if shell or s["function"]["name"] != "execute_shell_command")
        async def model(**kwargs):
            return {s["function"]["name"] for s in kwargs["tools"]}
        actual = await middleware.on_model_call(None, {"tools": SCHEMAS}, model)
        assert ("MinerU__parse_documents" in actual) is expected_legacy
        assert all((s["function"]["name"] in actual) is expected_legacy
                   for s in SCHEMAS if s["function"]["name"].startswith("MinerU__"))
        assert {"artifact_generate", "knowledge_search", "runtime_sandbox_files_search", "runtime_sandbox_files_select"} <= actual
        assert ("execute_shell_command" in actual) is shell
        assert "MinerU__parse_documents" in middleware.allowed_tool_names
    finally:
        reset_sandbox_tool_state(token)


@pytest.mark.asyncio
async def test_selection_recomputes_schema_and_unknown_current_file_keeps_extractors():
    scope = SandboxRequestScope("task", {"native_analysis_enabled": True, "isolation_level": "container",
        "analysis_environment": {"packages": {"openpyxl": "3.1.5"}}}, (), ())
    middleware = BankRuntimeGatewayMiddleware(None, native_analysis_enabled=True,
        sandbox_executor=SimpleNamespace(sandbox_context=scope.sandbox_context))
    middleware.allowed_tool_names = frozenset(s["function"]["name"] for s in SCHEMAS)
    async def model(**kwargs):
        return {s["function"]["name"] for s in kwargs["tools"]}
    token = set_sandbox_tool_state(SandboxToolState(scope, None, None, None))
    try:
        assert "MinerU__parse_documents" in await middleware.on_model_call(None, {"tools": SCHEMAS}, model)
        scope.sandbox_context['analysis_environment']['packages'] = {}
        scope.current_attachment_ids = ()
        scope.selected_file_ids = {'f1'}
        assert "MinerU__parse_documents" in await middleware.on_model_call(None, {"tools": SCHEMAS}, model)
        scope.sandbox_context['analysis_environment']['packages'] = {'openpyxl': '3.1.5'}
        scope.selected_file_ids.add("f1")
        scope.prepared_originals["f1"] = {"container_path": "/workspace/input/f1/" + "a"*64 + ".xlsx"}
        assert "MinerU__parse_documents" not in await middleware.on_model_call(None, {"tools": SCHEMAS}, model)
        scope.current_attachment_ids = ("unprepared",)
        assert "MinerU__parse_documents" in await middleware.on_model_call(None, {"tools": SCHEMAS}, model)
        scope.current_attachment_ids = ()
        scope.selected_file_ids.add("f2")
        scope.prepared_originals["f2"] = {"container_path": "/workspace/input/f2/" + "b"*64 + ".pdf"}
        assert "MinerU__parse_documents" in await middleware.on_model_call(None, {"tools": SCHEMAS}, model)
    finally:
        reset_sandbox_tool_state(token)


def test_environment_guidance_does_not_expose_authority_or_untrusted_configuration():
    from bank_runtime.sandbox.hooks import _analysis_environment_guidance
    context = {'execution_token': 'secret', 'signature': 'private',
        'expires_at': '2099-01-01T00:00:00Z', 'command_default_timeout_seconds': 300,
        'command_max_timeout_seconds': 1800, 'analysis_environment': {
            'python': '3.11.16', 'packages': {'openpyxl': '3.1.5', 'pandas': None, 'evil': 'execute me'},
            'cpu_count': 1, 'memory_bytes': 2*1024**3, 'work_bytes': 256*1024**2,
            'output_bytes': 1024, 'scratch_path': '/host/injected'}}
    actual = _analysis_environment_guidance(context)
    assert '3.11.16' in actual and '3.1.5' in actual and '2147483648' in actual
    assert '300' in actual and '1800' in actual and '2099-01-01' in actual
    assert not any(value in actual for value in ('secret', 'private', 'evil', '/host/injected'))
    assert _analysis_environment_guidance({'analysis_environment': 'bad'}) == ''


@pytest.mark.parametrize('environment', [None, 'bad', {'packages': None}, {'packages': 'bad'},
                                             {'packages': {}}, {'packages': {'openpyxl': None}}])
def test_unknown_environment_keeps_native_tables_and_explicit_missing_reader_keeps_fallback(environment):
    from bank_runtime.gateway.native_analysis import model_tool_names
    scope = SandboxRequestScope('task', {'native_analysis_enabled': True, 'isolation_level': 'container',
        'analysis_environment': environment}, (), ('f1',))
    scope.prepared_originals['f1'] = {'container_path': '/workspace/input/f1/' + 'a'*64 + '.xlsx'}
    token = set_sandbox_tool_state(SandboxToolState(scope, None, None, None))
    try:
        allowed = {'execute_shell_command', 'MinerU__parse_documents'}
        actual = model_tool_names(allowed, SimpleNamespace(sandbox_context=scope.sandbox_context))
        if environment == {'packages': {'openpyxl': None}}:
            assert actual == allowed
        else:
            assert actual == {'execute_shell_command'}
    finally:
        reset_sandbox_tool_state(token)
