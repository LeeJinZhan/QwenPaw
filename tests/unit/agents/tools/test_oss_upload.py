# -*- coding: utf-8 -*-
"""Tests for the built-in OSS upload tool."""
from __future__ import annotations

import importlib
import re
import sys
import types
from pathlib import Path

import pytest


class FakeAuth:
    def __init__(self, access_key_id: str, access_key_secret: str) -> None:
        self.access_key_id = access_key_id
        self.access_key_secret = access_key_secret


class FakeBucket:
    instances: list["FakeBucket"] = []

    def __init__(self, auth, endpoint: str, bucket_name: str) -> None:
        self.auth = auth
        self.endpoint = endpoint
        self.bucket_name = bucket_name
        self.put_calls: list[dict[str, object]] = []
        self.sign_calls: list[dict[str, object]] = []
        self.instances.append(self)

    def put_object_from_file(self, object_key: str, path: str, headers=None) -> None:
        self.put_calls.append(
            {"object_key": object_key, "path": path, "headers": headers},
        )

    def sign_url(
        self,
        method: str,
        object_key: str,
        expires: int,
        *,
        params=None,
        slash_safe: bool = False,
    ) -> str:
        self.sign_calls.append(
            {
                "method": method,
                "object_key": object_key,
                "expires": expires,
                "params": params,
                "slash_safe": slash_safe,
            },
        )
        return f"https://oss.example.com/{object_key}?signed=1"


def _text(response) -> str:
    return "\n".join(
        str(part.get("text", ""))
        if isinstance(part, dict)
        else str(getattr(part, "text", ""))
        for part in response.content
    )


def test_oss_upload_tool_is_exported() -> None:
    tools_module = importlib.import_module("qwenpaw.agents.tools")

    assert hasattr(tools_module, "upload_file_to_oss")
    assert "upload_file_to_oss" in tools_module.__all__


def test_oss_upload_tool_is_enabled_in_default_builtin_tools() -> None:
    config_module = importlib.import_module("qwenpaw.config.config")

    tool_config = config_module._default_builtin_tools()["upload_file_to_oss"]

    assert tool_config.enabled is True
    assert tool_config.description == "Upload a local file to Alibaba Cloud OSS"


@pytest.mark.asyncio
async def test_upload_file_to_oss_uses_environment_prefix_and_returns_signed_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = importlib.import_module("qwenpaw.agents.tools.oss_upload")
    fake_oss2 = types.SimpleNamespace(Auth=FakeAuth, Bucket=FakeBucket)
    source_file = tmp_path / "客户报告.xlsx"
    source_file.write_bytes(b"contents")
    FakeBucket.instances = []
    monkeypatch.setitem(sys.modules, "oss2", fake_oss2)
    monkeypatch.setenv("OSS_ENDPOINT", "https://oss-cn-hangzhou.aliyuncs.com")
    monkeypatch.setenv("OSS_BUCKET", "bank-agent-artifacts")
    monkeypatch.setenv("OSS_ACCESS_KEY_ID", "test-ak")
    monkeypatch.setenv("OSS_ACCESS_KEY_SECRET", "test-sk")
    monkeypatch.setenv("QWENPAW_OSS_UPLOAD_PREFIX", "exports/daily")
    monkeypatch.setenv("OSS_DOWNLOAD_URL_EXPIRES", "600")

    response = await module.upload_file_to_oss(
        str(source_file),
        filename="客户报告.xlsx",
    )

    assert _text(response).startswith("OSS upload succeeded: https://oss.example.com/")
    bucket = FakeBucket.instances[0]
    assert bucket.endpoint == "https://oss-cn-hangzhou.aliyuncs.com"
    assert bucket.bucket_name == "bank-agent-artifacts"
    uploaded_key = bucket.put_calls[0]["object_key"]
    assert isinstance(uploaded_key, str)
    assert re.fullmatch(r"exports/daily/[0-9a-f]{32}\.xlsx", uploaded_key)
    assert bucket.put_calls[0] == {
        "object_key": uploaded_key,
        "path": str(source_file.resolve()),
        "headers": {
            "Content-Disposition": (
                "attachment; filename=\"download.xlsx\"; "
                "filename*=UTF-8''%E5%AE%A2%E6%88%B7%E6%8A%A5%E5%91%8A.xlsx"
            ),
            "Content-Type": (
                "application/vnd.openxmlformats-officedocument."
                "spreadsheetml.sheet"
            ),
        },
    }
    assert bucket.sign_calls == [
        {
            "method": "GET",
            "object_key": uploaded_key,
            "expires": 600,
            "params": {
                "response-content-type": (
                    "application/vnd.openxmlformats-officedocument."
                    "spreadsheetml.sheet"
                ),
                "response-content-disposition": (
                    "attachment; filename=\"download.xlsx\"; "
                    "filename*=UTF-8''%E5%AE%A2%E6%88%B7%E6%8A%A5%E5%91%8A.xlsx"
                ),
            },
            "slash_safe": True,
        },
    ]


@pytest.mark.asyncio
async def test_upload_file_to_oss_reports_missing_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = importlib.import_module("qwenpaw.agents.tools.oss_upload")
    monkeypatch.delenv("OSS_ENDPOINT", raising=False)
    monkeypatch.delenv("OSS_BUCKET", raising=False)
    monkeypatch.delenv("OSS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("OSS_ACCESS_KEY_SECRET", raising=False)

    response = await module.upload_file_to_oss(str(tmp_path / "report.xlsx"))

    assert _text(response) == (
        "OSS upload failed: missing configuration "
        "OSS_ENDPOINT, OSS_BUCKET, OSS_ACCESS_KEY_ID, OSS_ACCESS_KEY_SECRET"
    )
