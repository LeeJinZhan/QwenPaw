# -*- coding: utf-8 -*-
"""Tests for qwenpaw.agents.tool_guard_mixin.

Covers:
- _normalize_tool_guard_ui_lang
- _tool_guard_t
- _GuardAction
- ToolGuardMixin._should_require_approval
- ToolGuardMixin._get_tool_execution_level
- ToolGuardMixin._tool_guard_ui_lang
- ToolGuardMixin._decide_guard_action
- ToolGuardMixin._create_info_guard_result
- ToolGuardMixin._severity_emoji_and_localized_name
- ToolGuardMixin._acting (partial)
"""
# pylint: disable=protected-access,unused-argument

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from qwenpaw.agents.tool_guard_mixin import (
    _GuardAction,
    _normalize_tool_guard_ui_lang,
    _tool_guard_t,
)
from qwenpaw.agents.runtime_tool_gateway import RuntimeToolGatewayError
from qwenpaw.agents.sandbox_executor_client import SandboxExecutorClientError
from qwenpaw.config.context import get_current_runtime_tool_execution
from qwenpaw.security.tool_guard.approval import ApprovalDecision
from qwenpaw.security.tool_guard.execution_level import ToolExecutionLevel
from qwenpaw.security.tool_guard.models import (
    GuardSeverity,
    GuardThreatCategory,
    ToolGuardResult,
)


# ---------------------------------------------------------------------------
# Helper to create a test mixin instance
# ---------------------------------------------------------------------------


def _make_mixin(**overrides):
    """Create a ToolGuardMixin instance with injected dependencies."""
    from qwenpaw.agents.tool_guard_mixin import ToolGuardMixin

    instance = ToolGuardMixin()

    # Inject required attributes
    instance._tool_guard_engine = MagicMock()
    instance._tool_guard_approval_service = MagicMock()
    instance._tool_guard_pending_info = None
    instance._tool_guard_lock = asyncio.Lock()
    instance._request_context = overrides.pop(
        "_request_context",
        {"session_id": "test-session"},
    )
    instance._agent_config = overrides.pop("_agent_config", None)
    instance._language = overrides.pop("_language", "en")
    instance.name = "TestAgent"
    instance.memory = MagicMock()

    for k, v in overrides.items():
        setattr(instance, k, v)

    return instance


# ---------------------------------------------------------------------------
# _normalize_tool_guard_ui_lang
# ---------------------------------------------------------------------------


class TestNormalizeToolGuardUiLang:
    """Tests for _normalize_tool_guard_ui_lang."""

    def test_en(self):
        assert _normalize_tool_guard_ui_lang("en") == "en"

    def test_zh(self):
        assert _normalize_tool_guard_ui_lang("zh") == "zh"

    def test_ru(self):
        assert _normalize_tool_guard_ui_lang("ru") == "ru"

    def test_ja(self):
        assert _normalize_tool_guard_ui_lang("ja") == "ja"

    def test_zh_cn_prefix(self):
        assert _normalize_tool_guard_ui_lang("zh-CN") == "zh"

    def test_unknown_defaults_to_en(self):
        assert _normalize_tool_guard_ui_lang("fr") == "en"

    def test_empty_defaults_to_en(self):
        assert _normalize_tool_guard_ui_lang("") == "en"

    def test_none_defaults_to_en(self):
        assert _normalize_tool_guard_ui_lang(None) == "en"

    def test_whitespace_stripped(self):
        assert _normalize_tool_guard_ui_lang("  en  ") == "en"


# ---------------------------------------------------------------------------
# _tool_guard_t
# ---------------------------------------------------------------------------


class TestToolGuardT:
    """Tests for _tool_guard_t."""

    def test_returns_string(self):
        result = _tool_guard_t("en", "tool_blocked")
        assert isinstance(result, str)

    def test_fallback_to_en(self):
        result = _tool_guard_t("xx", "tool_blocked")
        assert isinstance(result, str)

    def test_unknown_key_returns_key(self):
        result = _tool_guard_t("en", "nonexistent_key_xyz")
        assert result == "nonexistent_key_xyz"


# ---------------------------------------------------------------------------
# _GuardAction
# ---------------------------------------------------------------------------


class TestGuardAction:
    """Tests for _GuardAction."""

    def test_init(self):
        action = _GuardAction(
            "auto_denied",
            "rm",
            {"command": "rm -rf /"},
            guard_result=MagicMock(),
        )
        assert action.kind == "auto_denied"
        assert action.tool_name == "rm"
        assert action.tool_input == {"command": "rm -rf /"}
        assert action.guard_result is not None

    def test_default_guard_result(self):
        action = _GuardAction("needs_approval", "ls", {})
        assert action.guard_result is None


# ---------------------------------------------------------------------------
# ToolGuardMixin._should_require_approval
# ---------------------------------------------------------------------------


class TestShouldRequireApproval:
    """Tests for _should_require_approval."""

    def test_with_session_id(self):
        m = _make_mixin(_request_context={"session_id": "s1"})
        assert m._should_require_approval() is True

    def test_without_session_id(self):
        m = _make_mixin(_request_context={})
        assert m._should_require_approval() is False

    def test_empty_session_id(self):
        m = _make_mixin(_request_context={"session_id": ""})
        assert m._should_require_approval() is False


# ---------------------------------------------------------------------------
# ToolGuardMixin._get_tool_execution_level
# ---------------------------------------------------------------------------


class TestGetToolExecutionLevel:
    """Tests for _get_tool_execution_level."""

    def test_no_config_returns_auto(self):
        m = _make_mixin(_agent_config=None)
        assert m._get_tool_execution_level() == ToolExecutionLevel.AUTO

    def test_dict_config_strict(self):
        m = _make_mixin(_agent_config={"approval_level": "STRICT"})
        assert m._get_tool_execution_level() == ToolExecutionLevel.STRICT

    def test_dict_config_smart(self):
        m = _make_mixin(_agent_config={"approval_level": "smart"})
        assert m._get_tool_execution_level() == ToolExecutionLevel.SMART

    def test_pydantic_config(self):
        mock_config = MagicMock()
        mock_config.approval_level = "OFF"
        # Make getattr work normally for non-approval_level attrs
        del mock_config.__getitem__
        m = _make_mixin(_agent_config=mock_config)
        assert m._get_tool_execution_level() == ToolExecutionLevel.OFF

    def test_invalid_defaults_to_auto(self):
        m = _make_mixin(_agent_config={"approval_level": "invalid"})
        assert m._get_tool_execution_level() == ToolExecutionLevel.AUTO


# ---------------------------------------------------------------------------
# ToolGuardMixin._tool_guard_ui_lang
# ---------------------------------------------------------------------------


class TestToolGuardUiLang:
    """Tests for _tool_guard_ui_lang."""

    def test_with_language(self):
        m = _make_mixin(_language="zh-CN")
        assert m._tool_guard_ui_lang() == "zh"

    def test_without_language(self):
        m = _make_mixin(_language=None)
        assert m._tool_guard_ui_lang() == "en"

    def test_empty_language(self):
        m = _make_mixin(_language="")
        assert m._tool_guard_ui_lang() == "en"


# ---------------------------------------------------------------------------
# ToolGuardMixin._decide_guard_action
# ---------------------------------------------------------------------------


class TestDecideGuardAction:
    """Tests for _decide_guard_action."""

    @pytest.mark.asyncio
    async def test_empty_tool_name_returns_none(self):
        m = _make_mixin()
        m._tool_guard_engine.enabled = True
        result = await m._decide_guard_action({"name": "", "input": {}})
        assert result is None

    @pytest.mark.asyncio
    async def test_engine_disabled_returns_none(self):
        m = _make_mixin()
        m._tool_guard_engine.enabled = False
        result = await m._decide_guard_action(
            {"name": "rm", "input": {}},
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_off_mode_returns_none(self):
        m = _make_mixin(_agent_config={"approval_level": "OFF"})
        m._tool_guard_engine.enabled = True
        result = await m._decide_guard_action(
            {"name": "rm", "input": {}},
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_denied_tool_auto_denies(self):
        m = _make_mixin(_agent_config={"approval_level": "AUTO"})
        m._tool_guard_engine.enabled = True
        m._tool_guard_engine.is_denied.return_value = True
        m._tool_guard_engine.guard.return_value = MagicMock(
            findings=[MagicMock()],
        )
        result = await m._decide_guard_action(
            {"name": "dangerous_tool", "input": {}},
        )
        assert result is not None
        assert result.kind == "auto_denied"

    @pytest.mark.asyncio
    async def test_strict_mode_needs_approval(self):
        m = _make_mixin(
            _agent_config={"approval_level": "STRICT"},
            _request_context={"session_id": "s1"},
        )
        m._tool_guard_engine.enabled = True
        m._tool_guard_engine.is_denied.return_value = False
        m._tool_guard_engine.guard.return_value = MagicMock(
            findings=[],
            max_severity=GuardSeverity.INFO,
        )
        m._tool_guard_engine.should_auto_deny_result.return_value = False
        result = await m._decide_guard_action(
            {"name": "any_tool", "input": {}},
        )
        assert result is not None
        assert result.kind == "needs_approval"

    @pytest.mark.asyncio
    async def test_auto_mode_no_findings_returns_none(self):
        m = _make_mixin(_agent_config={"approval_level": "AUTO"})
        m._tool_guard_engine.enabled = True
        m._tool_guard_engine.is_denied.return_value = False
        m._tool_guard_engine.is_guarded.return_value = True
        m._tool_guard_engine.guard.return_value = MagicMock(
            findings=[],
        )
        result = await m._decide_guard_action(
            {"name": "safe_tool", "input": {}},
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_auto_mode_with_findings_auto_denies(self):
        m = _make_mixin(_agent_config={"approval_level": "AUTO"})
        m._tool_guard_engine.enabled = True
        m._tool_guard_engine.is_denied.return_value = False
        m._tool_guard_engine.is_guarded.return_value = True
        m._tool_guard_engine.guard.return_value = MagicMock(
            findings=[MagicMock()],
            max_severity=GuardSeverity.CRITICAL,
        )
        m._tool_guard_engine.should_auto_deny_result.return_value = True
        result = await m._decide_guard_action(
            {"name": "risky_tool", "input": {}},
        )
        assert result is not None
        assert result.kind == "auto_denied"

    @pytest.mark.asyncio
    async def test_smart_mode_low_risk_auto_allows(self):
        m = _make_mixin(_agent_config={"approval_level": "SMART"})
        m._tool_guard_engine.enabled = True
        m._tool_guard_engine.is_denied.return_value = False
        m._tool_guard_engine.is_guarded.return_value = True
        m._tool_guard_engine.guard.return_value = MagicMock(
            findings=[MagicMock()],
            max_severity=GuardSeverity.LOW,
        )
        m._tool_guard_engine.should_auto_deny_result.return_value = False
        result = await m._decide_guard_action(
            {"name": "low_risk_tool", "input": {}},
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_smart_mode_high_risk_needs_approval(self):
        m = _make_mixin(
            _agent_config={"approval_level": "SMART"},
            _request_context={"session_id": "s1"},
        )
        m._tool_guard_engine.enabled = True
        m._tool_guard_engine.is_denied.return_value = False
        m._tool_guard_engine.is_guarded.return_value = True
        m._tool_guard_engine.guard.return_value = MagicMock(
            findings=[MagicMock()],
            max_severity=GuardSeverity.HIGH,
        )
        m._tool_guard_engine.should_auto_deny_result.return_value = False
        result = await m._decide_guard_action(
            {"name": "high_risk_tool", "input": {}},
        )
        assert result is not None
        assert result.kind == "needs_approval"

    @pytest.mark.asyncio
    async def test_auto_mode_no_session_no_approval(self):
        m = _make_mixin(
            _agent_config={"approval_level": "AUTO"},
            _request_context={},
        )
        m._tool_guard_engine.enabled = True
        m._tool_guard_engine.is_denied.return_value = False
        m._tool_guard_engine.is_guarded.return_value = True
        m._tool_guard_engine.guard.return_value = MagicMock(
            findings=[MagicMock()],
            max_severity=GuardSeverity.HIGH,
        )
        m._tool_guard_engine.should_auto_deny_result.return_value = False
        # No session_id → cannot require approval → returns None
        result = await m._decide_guard_action(
            {"name": "tool", "input": {}},
        )
        assert result is None


# ---------------------------------------------------------------------------
# ToolGuardMixin._create_info_guard_result
# ---------------------------------------------------------------------------


class TestCreateInfoGuardResult:
    """Tests for _create_info_guard_result."""

    def test_creates_result_with_info_finding(self):
        m = _make_mixin()
        result = m._create_info_guard_result("rm", {"command": "rm -rf"})
        assert isinstance(result, ToolGuardResult)
        assert result.tool_name == "rm"
        assert len(result.findings) == 1
        assert result.findings[0].severity == GuardSeverity.INFO
        assert result.findings[0].rule_id == "strict_mode"

    def test_finding_has_correct_category(self):
        m = _make_mixin()
        result = m._create_info_guard_result("ls", {})
        assert (
            result.findings[0].category == GuardThreatCategory.RESOURCE_ABUSE
        )


# ---------------------------------------------------------------------------
# ToolGuardMixin._severity_emoji_and_localized_name
# ---------------------------------------------------------------------------


class TestSeverityEmojiAndLocalizedName:
    """Tests for _severity_emoji_and_localized_name."""

    def test_critical_emoji(self):
        from qwenpaw.agents.tool_guard_mixin import ToolGuardMixin

        emoji, _ = ToolGuardMixin._severity_emoji_and_localized_name(
            GuardSeverity.CRITICAL,
            "en",
        )
        assert emoji == "\U0001f534"

    def test_high_emoji(self):
        from qwenpaw.agents.tool_guard_mixin import ToolGuardMixin

        emoji, _ = ToolGuardMixin._severity_emoji_and_localized_name(
            GuardSeverity.HIGH,
            "en",
        )
        assert emoji == "\U0001f534"

    def test_medium_emoji(self):
        from qwenpaw.agents.tool_guard_mixin import ToolGuardMixin

        emoji, _ = ToolGuardMixin._severity_emoji_and_localized_name(
            GuardSeverity.MEDIUM,
            "en",
        )
        assert emoji == "\U0001f7e1"

    def test_returns_localized_name(self):
        from qwenpaw.agents.tool_guard_mixin import ToolGuardMixin

        _, loc_name = ToolGuardMixin._severity_emoji_and_localized_name(
            GuardSeverity.HIGH,
            "en",
        )
        assert isinstance(loc_name, str)
        assert len(loc_name) > 0


# ---------------------------------------------------------------------------
# ToolGuardMixin._ensure_tool_guard
# ---------------------------------------------------------------------------


class TestEnsureToolGuard:
    """Tests for _ensure_tool_guard."""

    def test_already_initialized(self):
        m = _make_mixin()
        # _tool_guard_engine already set by _make_mixin
        m._ensure_tool_guard()
        # Should not re-init

    @patch("qwenpaw.security.tool_guard.engine.get_guard_engine")
    @patch("qwenpaw.app.approvals.get_approval_service")
    def test_lazy_init(self, mock_approval, mock_engine):
        mock_engine.return_value = MagicMock()
        mock_approval.return_value = MagicMock()
        m = _make_mixin()
        # Remove the injected attributes to trigger lazy init
        del m._tool_guard_engine
        m._ensure_tool_guard()
        assert hasattr(m, "_tool_guard_engine")
        assert hasattr(m, "_tool_guard_approval_service")
        assert hasattr(m, "_tool_guard_lock")


# ---------------------------------------------------------------------------
# Runtime Tool Gateway execution boundary
# ---------------------------------------------------------------------------


class TestRuntimeToolGatewayExecution:
    """The native tool executes only after Runtime preflight allows it."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "tool_name",
        ["runtime_sandbox_files_search", "runtime_sandbox_files_select"],
    )
    async def test_runtime_sandbox_broker_tools_use_dedicated_runtime_authorization(
        self,
        tool_name,
    ):
        m = _make_mixin()
        gateway = MagicMock()
        gateway.preflight = AsyncMock(
            side_effect=AssertionError(
                "sandbox broker tools must not use the generic Tool Gateway",
            ),
        )
        m._runtime_tool_gateway_client = MagicMock(return_value=gateway)
        m._call_parent_tool = AsyncMock(return_value={"status": "authorized"})
        tool_call = {
            "id": "call_sandbox_001",
            "name": tool_name,
            "input": {"file_id": "file_001"},
        }

        result = await m._acting(tool_call)

        assert result == {"status": "authorized"}
        gateway.preflight.assert_not_awaited()
        m._call_parent_tool.assert_awaited_once_with(tool_call)

    @pytest.mark.asyncio
    async def test_gateway_denial_returns_tool_failure_without_execution(self):
        m = _make_mixin()
        gateway = MagicMock()
        gateway.preflight = AsyncMock(side_effect=RuntimeToolGatewayError("denied"))
        m._runtime_tool_gateway_client = MagicMock(return_value=gateway)
        m._call_parent_tool = AsyncMock()
        m._emit_runtime_gateway_failure = AsyncMock()

        result = await m._execute_runtime_gateway_tool_call(
            {"id": "call_001", "name": "write_file", "input": {"path": "output/a.txt"}},
        )

        assert result is None
        m._call_parent_tool.assert_not_awaited()
        m._emit_runtime_gateway_failure.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_gateway_denial_exposes_only_public_summary_to_agent(self):
        m = _make_mixin()
        gateway = MagicMock()
        gateway.preflight = AsyncMock(
            side_effect=RuntimeToolGatewayError(
                "internal policy detail",
                code="POLICY_BLOCKED",
                violation_type="assistant_tool_not_allowed",
                public_summary="当前助手未授权使用该工具，请尝试其他方式完成请求。",
            ),
        )
        m._runtime_tool_gateway_client = MagicMock(return_value=gateway)
        m._call_parent_tool = AsyncMock()
        m._emit_runtime_gateway_failure = AsyncMock()
        tool_call = {"id": "call_001", "name": "execute_shell_command", "input": {"command": "pwd"}}

        result = await m._execute_runtime_gateway_tool_call(tool_call)

        assert result is None
        m._call_parent_tool.assert_not_awaited()
        m._emit_runtime_gateway_failure.assert_awaited_once_with(
            tool_call,
            "当前助手未授权使用该工具，请尝试其他方式完成请求。",
        )

    @pytest.mark.asyncio
    async def test_gateway_allow_executes_native_tool_then_reports_status_only(self):
        m = _make_mixin()
        gateway = MagicMock()
        gateway.preflight = AsyncMock(return_value={"tool_call_id": "tool_001"})
        gateway.report_guard = AsyncMock(return_value={"status": "executing"})
        gateway.report_result = AsyncMock(return_value={"status": "completed"})
        m._runtime_tool_gateway_client = MagicMock(return_value=gateway)
        m._call_parent_tool = AsyncMock(return_value={"sensitive_native_output": "never audited"})
        m._emit_runtime_gateway_failure = AsyncMock()
        tool_call = {"id": "call_001", "name": "write_file", "input": {"path": "output/a.txt"}}

        result = await m._execute_runtime_gateway_tool_call(tool_call)

        assert result == {"sensitive_native_output": "never audited"}
        gateway.preflight.assert_awaited_once_with(
            "write_file",
            {"path": "output/a.txt"},
            idempotency_key="qwenpaw:call_001",
        )
        gateway.report_result.assert_awaited_once()
        report_args = gateway.report_result.await_args.args
        assert report_args[0] == "tool_001"
        assert report_args[1] == "completed"
        assert "sensitive_native_output" not in str(gateway.report_result.await_args)

    @pytest.mark.asyncio
    async def test_gateway_reports_failed_when_runtime_sandbox_execution_raises(self):
        m = _make_mixin()
        gateway = MagicMock()
        gateway.preflight = AsyncMock(return_value={"tool_call_id": "tool_001"})
        gateway.report_guard = AsyncMock(return_value={"status": "executing"})
        gateway.report_result = AsyncMock(return_value={"status": "failed"})
        m._runtime_tool_gateway_client = MagicMock(return_value=gateway)
        m._call_parent_tool = AsyncMock(
            side_effect=SandboxExecutorClientError("Runtime sandbox operation failed"),
        )
        tool_call = {
            "id": "call_001",
            "name": "execute_shell_command",
            "input": {"command": "pwd"},
        }

        with pytest.raises(SandboxExecutorClientError):
            await m._execute_runtime_gateway_tool_call(tool_call)

        gateway.report_result.assert_awaited_once()
        report_args = gateway.report_result.await_args.args
        assert report_args[0] == "tool_001"
        assert report_args[1] == "failed"
        assert report_args[3] == "TOOL_EXECUTION_FAILED"

    @pytest.mark.asyncio
    async def test_gateway_uses_runtime_tool_id_for_sandbox_execution(self):
        m = _make_mixin()
        gateway = MagicMock()
        gateway.report_result = AsyncMock(return_value={"status": "completed"})
        observed: dict[str, object] = {}

        async def execute_native(_tool_call):
            observed.update(get_current_runtime_tool_execution() or {})
            return {"status": "ok"}

        m._call_parent_tool = AsyncMock(side_effect=execute_native)
        tool_call = {
            "id": "call_shell_001",
            "name": "execute_shell_command",
            "input": {"command": "pwd"},
        }
        preflight = {
            "tool_call_id": "tool_001",
            "permit": {
                "payload": {
                    "tool_id": "execute_shell_command",
                    "runtime_tool_id": "shell.exec",
                },
            },
        }

        result = await m._execute_runtime_gateway_tool_call(
            tool_call,
            gateway_client=gateway,
            preflight=preflight,
        )

        assert result == {"status": "ok"}
        assert observed["tool_call_id"] == "tool_001"
        assert observed["tool_name"] == "shell.exec"
        assert observed["worker_tool_name"] == "execute_shell_command"
        assert observed["tool_input"] == {"command": "pwd"}

    @pytest.mark.asyncio
    async def test_runtime_gateway_cannot_bypass_guard_when_headless_flag_is_false(self):
        m = _make_mixin(_request_context={"_headless_tool_guard": "false"})
        gateway = MagicMock()
        gateway.preflight = AsyncMock(return_value={"tool_call_id": "tool_001"})
        gateway.report_guard = AsyncMock(return_value={"status": "executing"})
        m._runtime_tool_gateway_client = MagicMock(return_value=gateway)
        m._decide_guard_action = AsyncMock(return_value=None)
        m._execute_runtime_gateway_tool_call = AsyncMock(return_value={"status": "ok"})
        m._emit_runtime_gateway_failure = AsyncMock()
        tool_call = {"id": "call_001", "name": "execute_shell_command", "input": {"command": "pwd"}}

        result = await m._acting(tool_call)

        assert result == {"status": "ok"}
        gateway.preflight.assert_awaited_once_with(
            "execute_shell_command",
            {"command": "pwd"},
            idempotency_key="qwenpaw:call_001",
        )
        gateway.report_guard.assert_awaited_once_with("tool_001", "allow")
        m._execute_runtime_gateway_tool_call.assert_awaited_once_with(
            tool_call,
            gateway_client=gateway,
            preflight={"tool_call_id": "tool_001"},
        )

    @pytest.mark.asyncio
    async def test_runtime_gateway_guard_report_failure_blocks_execution(self):
        m = _make_mixin()
        gateway = MagicMock()
        gateway.preflight = AsyncMock(return_value={"tool_call_id": "tool_001"})
        gateway.report_guard = AsyncMock(side_effect=RuntimeToolGatewayError("offline"))
        m._runtime_tool_gateway_client = MagicMock(return_value=gateway)
        m._decide_guard_action = AsyncMock(return_value=None)
        m._execute_runtime_gateway_tool_call = AsyncMock(return_value={"status": "ok"})
        m._emit_runtime_gateway_failure = AsyncMock()
        tool_call = {"id": "call_001", "name": "read_file", "input": {}}

        result = await m._acting(tool_call)

        assert result is None
        m._execute_runtime_gateway_tool_call.assert_not_awaited()
        m._emit_runtime_gateway_failure.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_runtime_gateway_local_guard_failure_blocks_execution(self):
        m = _make_mixin()
        gateway = MagicMock()
        gateway.preflight = AsyncMock(return_value={"tool_call_id": "tool_001"})
        gateway.report_guard = AsyncMock()
        m._runtime_tool_gateway_client = MagicMock(return_value=gateway)
        m._decide_guard_action = AsyncMock(side_effect=RuntimeError("guard unavailable"))
        m._execute_runtime_gateway_tool_call = AsyncMock()
        m._emit_runtime_gateway_failure = AsyncMock()
        tool_call = {"id": "call_001", "name": "read_file", "input": {}}

        result = await m._acting(tool_call)

        assert result is None
        gateway.report_guard.assert_awaited_once_with("tool_001", "block")
        m._execute_runtime_gateway_tool_call.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_auto_denied_guard_reuses_gateway_context_without_type_error(self):
        m = _make_mixin()
        m.print = AsyncMock()
        m.memory.add = AsyncMock()
        gateway = MagicMock()
        gateway.report_guard = AsyncMock()
        tool_call = {
            "id": "call_001",
            "name": "execute_shell_command",
            "input": {"command": "rm -rf /tmp/example"},
        }

        result = await m._execute_guard_action(
            _GuardAction(
                "auto_denied",
                "execute_shell_command",
                tool_call["input"],
            ),
            tool_call,
            gateway_client=gateway,
            gateway_preflight={"tool_call_id": "tool_001"},
        )

        assert result is None
        m.print.assert_awaited_once()
        m.memory.add.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_runtime_gateway_reports_required_approval_before_waiting(self):
        m = _make_mixin(_request_context={"session_id": "s1"})
        gateway = MagicMock()
        gateway.preflight = AsyncMock(return_value={"tool_call_id": "tool_001"})
        gateway.report_guard = AsyncMock(
            return_value={
                "guard_decision": "require_approval",
                "status": "pending_approval",
            },
        )
        m._runtime_tool_gateway_client = MagicMock(return_value=gateway)
        action = _GuardAction(
            "needs_approval",
            "write_file",
            {"path": "output/a.txt"},
        )
        m._decide_guard_action = AsyncMock(return_value=action)
        m._execute_guard_action = AsyncMock(return_value={"status": "pending"})
        m._execute_runtime_gateway_tool_call = AsyncMock()
        m._emit_runtime_gateway_failure = AsyncMock()
        tool_call = {"id": "call_001", "name": "write_file", "input": {"path": "output/a.txt"}}

        result = await m._acting(tool_call)

        assert result == {"status": "pending"}
        gateway.report_guard.assert_awaited_once_with(
            "tool_001",
            "require_approval",
        )
        m._execute_guard_action.assert_awaited_once_with(
            action,
            tool_call,
            gateway_client=gateway,
            gateway_preflight={"tool_call_id": "tool_001"},
        )
        m._execute_runtime_gateway_tool_call.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("decision", "expected_guard_decision", "terminal_method"),
        [
            (ApprovalDecision.APPROVED, "allow", "execute"),
            (ApprovalDecision.DENIED, "block", "denied"),
            (ApprovalDecision.TIMEOUT, "block", "timeout"),
        ],
    )
    async def test_approval_guard_reuses_original_gateway_preflight(
        self,
        decision,
        expected_guard_decision,
        terminal_method,
    ):
        m = _make_mixin()
        pending = MagicMock(request_id="approval_001", future=MagicMock())
        m._tool_guard_approval_service.cancel_stale_pending_for_tool_call = AsyncMock()
        m._tool_guard_approval_service.create_pending = AsyncMock(return_value=pending)
        m._emit_waiting_for_approval_blocking = AsyncMock()
        m._wait_for_approval_with_heartbeat = AsyncMock(return_value=decision)
        m._execute_runtime_gateway_tool_call = AsyncMock(return_value={"status": "ok"})
        m._acting_denied = AsyncMock(return_value=None)
        m._acting_timeout = AsyncMock(return_value=None)
        gateway = MagicMock()
        gateway.report_guard = AsyncMock()
        preflight = {"tool_call_id": "tool_001"}
        tool_call = {
            "id": "call_001",
            "name": "execute_shell_command",
            "input": {"command": "pwd"},
        }

        await m._execute_guard_action(
            _GuardAction(
                "needs_approval",
                "execute_shell_command",
                tool_call["input"],
            ),
            tool_call,
            gateway_client=gateway,
            gateway_preflight=preflight,
        )

        gateway.report_guard.assert_awaited_once_with(
            "tool_001",
            expected_guard_decision,
        )
        if terminal_method == "execute":
            m._execute_runtime_gateway_tool_call.assert_awaited_once_with(
                tool_call,
                gateway_client=gateway,
                preflight=preflight,
            )
        elif terminal_method == "denied":
            m._acting_denied.assert_awaited_once()
        else:
            m._acting_timeout.assert_awaited_once()
