# -*- coding: utf-8 -*-
"""Runner boundaries for request-scoped Personal Skills."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from qwenpaw.app.runner.query_error_dump import (
    _request_to_dict,
    write_query_error_dump,
)
from qwenpaw.app.runner.runner import _build_base_request_context


def test_runner_copies_catalog_and_private_manifest_into_request_context() -> None:
    catalog = {"snapshot_id": "pss_001", "items": [], "limits": {}}
    manifest = {
        "snapshot_id": "pss_001",
        "items": [
            {
                "skill_ref": "personal:skill_001",
                "signed_get_url": "https://oss.example.com/a.md?secret=token",
                "content_type": "text/markdown",
            },
        ],
    }

    context = _build_base_request_context(
        session_id="session_001",
        user_id="user_001",
        channel="bank-runtime",
        agent_id="default",
        channel_meta={
            "personal_skills_catalog": catalog,
            "personal_skills_access_manifest": manifest,
        },
        payload_context=None,
    )

    assert context["personal_skills_catalog"] == catalog
    assert context["personal_skills_access_manifest"] == manifest

    overridden = _build_base_request_context(
        session_id="session_001",
        user_id="user_001",
        channel="bank-runtime",
        agent_id="default",
        channel_meta={
            "personal_skills_catalog": catalog,
            "personal_skills_access_manifest": manifest,
        },
        payload_context={
            "personal_skills_catalog": {"snapshot_id": "attacker"},
            "personal_skills_access_manifest": {"snapshot_id": "attacker"},
        },
    )
    assert overridden["personal_skills_catalog"] == catalog
    assert overridden["personal_skills_access_manifest"] == manifest


def test_query_error_dump_redacts_private_access_manifest() -> None:
    secret_url = "https://oss.example.com/a.md?secret=token"
    request = SimpleNamespace(
        session_id="session_001",
        user_id="user_001",
        channel="bank-runtime",
        channel_meta={
            "personal_skills_catalog": {"snapshot_id": "pss_001"},
            "personal_skills_access_manifest": {
                "items": [{"signed_get_url": secret_url}],
            },
        },
    )

    dumped = _request_to_dict(request)

    assert secret_url not in str(dumped)
    assert dumped["channel_meta"]["personal_skills_access_manifest"] == "[REDACTED]"


def test_query_error_dump_redacts_activated_skill_from_agent_state(
    tmp_path, monkeypatch
) -> None:
    secret_body = "confidential request-scoped skill body"

    class Registry:
        @staticmethod
        def redact_for_persistence(state):
            return {"memory": str(state["memory"]).replace(secret_body, "[REDACTED]")}

    agent = SimpleNamespace(
        _personal_skills_registry=Registry(),
        state_dict=lambda: {"memory": secret_body},
    )
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))

    try:
        raise RuntimeError("failed")
    except RuntimeError as exc:
        path = write_query_error_dump(None, exc, {"agent": agent})

    content = Path(path).read_text(encoding="utf-8")
    assert secret_body not in content
    assert "[REDACTED]" in content
