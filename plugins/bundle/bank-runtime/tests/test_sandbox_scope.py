from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from bank_runtime.sandbox.scope import SandboxScopeError, SandboxRequestScope


def _request(**overrides):
    values = {
        "channel": "bank-runtime",
        "runtime_task_id": "task_001",
        "user_id": "user_001",
        "sandbox_context": {
            "context_id": "ctx_001",
            "task_id": "task_001",
            "signature": "signed",
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(),
        },
        "attachments_manifest": [
            {
                "file_id": "file_current",
                "source": "current_task",
                "original_name": "材料.txt",
                "content_type": "text/plain",
                "size_bytes": 12,
            }
        ],
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_scope_accepts_only_current_manifest_or_request_search_candidates() -> None:
    scope = SandboxRequestScope.from_request(_request())
    assert scope.current_attachment_ids == ("file_current",)

    scope.remember_discovered(
        [
            {
                "file_id": "file_history",
                "source": "conversation",
                "readable": True,
            },
            {
                "file_id": "file_unreadable",
                "source": "assistant_workspace",
                "readable": False,
            },
        ]
    )

    assert scope.selection_records(["file_history"]) == [
        {
            "file_id": "file_history",
            "source": "conversation",
            "selection_mode": "model_metadata_selection",
        }
    ]
    with pytest.raises(SandboxScopeError):
        scope.selection_records(["file_forged"])
    with pytest.raises(SandboxScopeError):
        scope.selection_records(["file_unreadable"])


def test_scope_accepts_runtime_opaque_context_manifest_reference() -> None:
    request = _request(
        sandbox_context={
            "context_manifest_id": "ctxm_001",
            "task_id": "task_001",
            "signature": "signed",
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(),
        }
    )

    scope = SandboxRequestScope.from_request(request)

    assert scope.sandbox_context["context_manifest_id"] == "ctxm_001"


@pytest.mark.parametrize(
    "candidate_request",
    [
        _request(sandbox_context=None),
        _request(sandbox_context={"task_id": "task_other", "signature": "signed"}),
        _request(attachments_manifest=[{"file_id": "../escape"}]),
        _request(
            attachments_manifest=[
                {"file_id": "file_duplicate", "source": "current_task"},
                {"file_id": "file_duplicate", "source": "current_task"},
            ]
        ),
    ],
)
def test_scope_rejects_missing_cross_task_malformed_or_duplicate_manifest(
    candidate_request,
) -> None:
    with pytest.raises(SandboxScopeError):
        SandboxRequestScope.from_request(candidate_request)


def test_scope_rejects_duplicate_or_excessive_model_selection() -> None:
    scope = SandboxRequestScope.from_request(_request())
    scope.remember_discovered(
        [
            {
                "file_id": f"file_{index}",
                "source": "conversation",
                "readable": True,
            }
            for index in range(5)
        ]
    )
    with pytest.raises(SandboxScopeError):
        scope.selection_records(["file_0", "file_0"])
    assert len(scope.selection_records(["file_0", "file_1", "file_2", "file_3"])) == 4
    scope.mark_selected(["file_0", "file_1", "file_2", "file_3"])
    with pytest.raises(SandboxScopeError):
        scope.selection_records(["file_4"])


def test_scope_limits_current_and_selected_files_to_five_per_task() -> None:
    request = _request(
        attachments_manifest=[
            {
                "file_id": f"file_current_{index}",
                "source": "current_task",
                "content_type": "text/plain",
                "size_bytes": 1,
            }
            for index in range(4)
        ]
    )
    scope = SandboxRequestScope.from_request(request)
    scope.remember_discovered(
        [
            {
                "file_id": "file_history_1",
                "source": "conversation",
                "readable": True,
            },
            {
                "file_id": "file_history_2",
                "source": "assistant_workspace",
                "readable": True,
            },
        ]
    )

    assert len(scope.selection_records(["file_history_1"])) == 1
    scope.mark_selected(["file_history_1"])
    with pytest.raises(SandboxScopeError, match="limit"):
        scope.selection_records(["file_history_2"])

    with pytest.raises(SandboxScopeError, match="manifest"):
        SandboxRequestScope.from_request(
            _request(
                attachments_manifest=[
                    {"file_id": f"file_{index}", "source": "current_task"}
                    for index in range(6)
                ]
            )
        )


def test_scope_public_metadata_never_retains_storage_locators() -> None:
    scope = SandboxRequestScope.from_request(_request())
    scope.remember_discovered(
        [
            {
                "file_id": "file_history",
                "display_name": "历史材料.pdf",
                "content_type": "application/pdf",
                "size_bytes": 42,
                "source": "conversation",
                "readable": True,
                "bucket": "secret-bucket",
                "object_key": "private/object",
                "read_url": "https://secret.invalid/object",
                "token": "secret",
                "workspace_path": "/host/private/object",
            }
        ]
    )

    rendered = repr(scope.discovered_files["file_history"])
    assert "secret-bucket" not in rendered
    assert "private/object" not in rendered
    assert "secret.invalid" not in rendered
    assert "/host/private" not in rendered
