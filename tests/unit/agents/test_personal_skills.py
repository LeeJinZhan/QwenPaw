# -*- coding: utf-8 -*-
"""Request-scoped Personal Skills protocol and loader tests."""

from __future__ import annotations

import hashlib

import pytest

from qwenpaw.agents.personal_skills import (
    DownloadedPersonalSkill,
    PersonalSkillProtocolError,
    PersonalSkillsRegistry,
    build_personal_skills_catalog_prompt,
)


def _skill_bytes(
    *,
    skill_id: str = "skill_001",
    name: str = "月报整理",
    description: str = "按照部门规范整理月度材料",
    when_to_use: str = "生成、汇总或修改月报时",
    body: str = "# 工作步骤\n\n1. 汇总本月进展。\n",
) -> bytes:
    return (
        "---\n"
        'schema_version: "1.0"\n'
        f'skill_id: "{skill_id}"\n'
        f'name: "{name}"\n'
        f'description: "{description}"\n'
        f'when_to_use: "{when_to_use}"\n'
        "---\n\n"
        f"{body}"
    ).encode("utf-8")


def _payloads(
    content: bytes | None = None,
    *,
    url: str = "https://runtime-test.oss.example.com/skill.md?secret=token",
    limits: dict | None = None,
) -> tuple[dict, dict]:
    content = content if content is not None else _skill_bytes()
    catalog = {
        "snapshot_id": "snapshot_001",
        "items": [
            {
                "skill_ref": "personal:skill_001",
                "skill_id": "skill_001",
                "source": "personal",
                "trust_level": "user",
                "name": "月报整理",
                "description": "按照部门规范整理月度材料",
                "when_to_use": "生成、汇总或修改月报时",
                "version_no": 3,
                "content_hash": hashlib.sha256(content).hexdigest(),
                "size_bytes": len(content),
            },
        ],
        "limits": limits
        or {
            "max_candidate_loads": 3,
            "max_activated": 3,
            "max_activated_bytes": 65536,
        },
    }
    manifest = {
        "snapshot_id": "snapshot_001",
        "items": [
            {
                "skill_ref": "personal:skill_001",
                "signed_get_url": url,
                "content_type": "text/markdown",
            },
        ],
    }
    return catalog, manifest


def _response_text(response) -> str:
    return "\n".join(
        str(block.get("text") or "")
        for block in response.content
        if isinstance(block, dict)
    )


def test_catalog_prompt_contains_selection_metadata_but_never_access_url() -> None:
    catalog, manifest = _payloads()

    prompt = build_personal_skills_catalog_prompt(catalog)

    assert "personal:skill_001" in prompt
    assert "月报整理" in prompt
    assert "生成、汇总或修改月报时" in prompt
    assert "activate_personal_skill" in prompt
    assert "signed_get_url" not in prompt
    assert manifest["items"][0]["signed_get_url"] not in prompt


@pytest.mark.asyncio
async def test_registry_does_not_download_until_model_activates_skill(
    monkeypatch,
) -> None:
    monkeypatch.setenv("PERSONAL_SKILLS_ALLOWED_OSS_HOSTS", "oss.example.com")
    content = _skill_bytes()
    catalog, manifest = _payloads(content)
    calls: list[str] = []

    async def fetch(url: str, max_bytes: int) -> DownloadedPersonalSkill:
        calls.append(url)
        return DownloadedPersonalSkill(
            content=content,
            final_url=url,
            redirected=False,
        )

    registry = PersonalSkillsRegistry.from_payloads(
        catalog,
        manifest,
        fetcher=fetch,
    )

    assert calls == []
    response = await registry.activate_personal_skill("personal:skill_001")

    assert calls == [manifest["items"][0]["signed_get_url"]]
    assert "汇总本月进展" in _response_text(response)
    assert registry.activated_skill_refs == ("personal:skill_001",)
    assert registry.pop_runtime_event() == {
        "event_type": "personal_skill.activated",
        "skill_id": "skill_001",
        "version_no": 3,
        "content_hash": hashlib.sha256(content).hexdigest(),
        "result": "activated",
        "duration_bucket": "lt_100ms",
    }


@pytest.mark.asyncio
async def test_activate_accepts_only_catalog_skill_ref_not_model_url(
    monkeypatch,
) -> None:
    monkeypatch.setenv("PERSONAL_SKILLS_ALLOWED_OSS_HOSTS", "oss.example.com")
    catalog, manifest = _payloads()
    calls = 0

    async def fetch(url: str, max_bytes: int) -> DownloadedPersonalSkill:
        nonlocal calls
        calls += 1
        raise AssertionError("unknown skill_ref must not download")

    registry = PersonalSkillsRegistry.from_payloads(
        catalog,
        manifest,
        fetcher=fetch,
    )

    response = await registry.activate_personal_skill(
        manifest["items"][0]["signed_get_url"],
    )

    assert calls == 0
    assert "could not be activated" in _response_text(response)
    assert registry.pop_runtime_event()["result"] == "unknown_skill_ref"


@pytest.mark.asyncio
async def test_loopback_runtime_signed_route_is_allowed_for_local_adapter() -> None:
    content = _skill_bytes()
    catalog, manifest = _payloads(
        content,
        url=(
            "http://127.0.0.1:8765/runtime/internal/"
            "personal-skill-content/signed-token"
        ),
    )

    async def fetch(url: str, max_bytes: int) -> DownloadedPersonalSkill:
        return DownloadedPersonalSkill(content=content, final_url=url, redirected=False)

    registry = PersonalSkillsRegistry.from_payloads(catalog, manifest, fetcher=fetch)
    response = await registry.activate_personal_skill("personal:skill_001")

    assert "汇总本月进展" in _response_text(response)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "mutate", "expected"),
    [
        (
            "http",
            lambda catalog, manifest, content: manifest["items"][0].update(
                signed_get_url="http://runtime-test.oss.example.com/skill.md",
            ),
            "HTTPS",
        ),
        (
            "host",
            lambda catalog, manifest, content: manifest["items"][0].update(
                signed_get_url="https://attacker.example.net/skill.md",
            ),
            "host",
        ),
        (
            "hash",
            lambda catalog, manifest, content: catalog["items"][0].update(
                content_hash="0" * 64,
            ),
            "hash",
        ),
        (
            "declared-size",
            lambda catalog, manifest, content: catalog["items"][0].update(
                size_bytes=len(content) + 1,
            ),
            "size",
        ),
        (
            "frontmatter",
            lambda catalog, manifest, content: catalog["items"][0].update(
                name="另一个名称",
            ),
            "frontmatter",
        ),
    ],
)
async def test_activation_rejects_invalid_content_or_locator(
    monkeypatch,
    case,
    mutate,
    expected,
) -> None:
    del case
    monkeypatch.setenv("PERSONAL_SKILLS_ALLOWED_OSS_HOSTS", "oss.example.com")
    content = _skill_bytes()
    catalog, manifest = _payloads(content)
    mutate(catalog, manifest, content)

    async def fetch(url: str, max_bytes: int) -> DownloadedPersonalSkill:
        return DownloadedPersonalSkill(
            content=content,
            final_url=url,
            redirected=False,
        )

    registry = PersonalSkillsRegistry.from_payloads(
        catalog,
        manifest,
        fetcher=fetch,
    )
    response = await registry.activate_personal_skill("personal:skill_001")

    assert expected.lower() in _response_text(response).lower()
    assert registry.activated_skill_refs == ()


@pytest.mark.asyncio
async def test_activation_rejects_redirect_invalid_utf8_and_32kb_overflow(
    monkeypatch,
) -> None:
    monkeypatch.setenv("PERSONAL_SKILLS_ALLOWED_OSS_HOSTS", "oss.example.com")
    valid = _skill_bytes()

    async def activate_with(downloaded: DownloadedPersonalSkill, declared: bytes):
        catalog, manifest = _payloads(declared)

        async def fetch(url: str, max_bytes: int) -> DownloadedPersonalSkill:
            return downloaded

        registry = PersonalSkillsRegistry.from_payloads(
            catalog,
            manifest,
            fetcher=fetch,
        )
        return await registry.activate_personal_skill("personal:skill_001")

    redirected = await activate_with(
        DownloadedPersonalSkill(
            content=valid,
            final_url="https://runtime-test.oss.example.com/other.md",
            redirected=True,
        ),
        valid,
    )
    invalid_utf8 = b"\xff\xfe"
    utf8_result = await activate_with(
        DownloadedPersonalSkill(
            content=invalid_utf8,
            final_url="https://runtime-test.oss.example.com/skill.md?secret=token",
            redirected=False,
        ),
        invalid_utf8,
    )
    oversized = b"x" * 32769
    oversized_result = await activate_with(
        DownloadedPersonalSkill(
            content=oversized,
            final_url="https://runtime-test.oss.example.com/skill.md?secret=token",
            redirected=False,
        ),
        oversized,
    )

    assert "redirect" in _response_text(redirected).lower()
    assert "utf-8" in _response_text(utf8_result).lower()
    assert "32kb" in _response_text(oversized_result).lower()


@pytest.mark.asyncio
async def test_activation_requires_supported_frontmatter_schema(monkeypatch) -> None:
    monkeypatch.setenv("PERSONAL_SKILLS_ALLOWED_OSS_HOSTS", "oss.example.com")
    content = _skill_bytes().replace(
        b'schema_version: "1.0"',
        b'schema_version: "2.0"',
    )
    catalog, manifest = _payloads(content)

    async def fetch(url: str, max_bytes: int) -> DownloadedPersonalSkill:
        return DownloadedPersonalSkill(content, url, False)

    registry = PersonalSkillsRegistry.from_payloads(
        catalog,
        manifest,
        fetcher=fetch,
    )

    response = await registry.activate_personal_skill("personal:skill_001")

    assert "schema_version" in _response_text(response)
    assert registry.activated_skill_refs == ()


@pytest.mark.asyncio
async def test_limits_bound_candidate_loads_activations_and_total_bytes(
    monkeypatch,
) -> None:
    monkeypatch.setenv("PERSONAL_SKILLS_ALLOWED_OSS_HOSTS", "oss.example.com")
    first = _skill_bytes()
    second = _skill_bytes(
        skill_id="skill_002",
        name="周报整理",
        description="整理周报",
        when_to_use="生成周报时",
    )
    catalog, manifest = _payloads(
        first,
        limits={
            "max_candidate_loads": 1,
            "max_activated": 1,
            "max_activated_bytes": len(first),
        },
    )
    catalog["items"].append(
        {
            "skill_ref": "personal:skill_002",
            "skill_id": "skill_002",
            "source": "personal",
            "trust_level": "user",
            "name": "周报整理",
            "description": "整理周报",
            "when_to_use": "生成周报时",
            "version_no": 1,
            "content_hash": hashlib.sha256(second).hexdigest(),
            "size_bytes": len(second),
        },
    )
    manifest["items"].append(
        {
            "skill_ref": "personal:skill_002",
            "signed_get_url": "https://runtime-test.oss.example.com/skill2.md",
            "content_type": "text/markdown",
        },
    )
    contents = {
        manifest["items"][0]["signed_get_url"]: first,
        manifest["items"][1]["signed_get_url"]: second,
    }
    calls: list[str] = []

    async def fetch(url: str, max_bytes: int) -> DownloadedPersonalSkill:
        calls.append(url)
        return DownloadedPersonalSkill(contents[url], url, False)

    registry = PersonalSkillsRegistry.from_payloads(
        catalog,
        manifest,
        fetcher=fetch,
    )
    first_response = await registry.activate_personal_skill("personal:skill_001")
    repeated_response = await registry.activate_personal_skill("personal:skill_001")
    second_response = await registry.activate_personal_skill("personal:skill_002")

    assert "汇总本月进展" in _response_text(first_response)
    assert "汇总本月进展" in _response_text(repeated_response)
    assert "candidate load limit" in _response_text(second_response).lower()
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("limits", "expected"),
    [
        (
            {
                "max_candidate_loads": 3,
                "max_activated": 1,
                "max_activated_bytes": 65536,
            },
            "activation limit",
        ),
        (
            {
                "max_candidate_loads": 3,
                "max_activated": 3,
                "max_activated_bytes": 1,
            },
            "activated byte limit",
        ),
    ],
)
async def test_activation_and_total_byte_limits_are_enforced_before_download(
    monkeypatch,
    limits,
    expected,
) -> None:
    monkeypatch.setenv("PERSONAL_SKILLS_ALLOWED_OSS_HOSTS", "oss.example.com")
    content = _skill_bytes()
    catalog, manifest = _payloads(content, limits=limits)
    if expected == "activation limit":
        second_item = dict(catalog["items"][0])
        second_item.update(
            skill_ref="personal:skill_002",
            skill_id="skill_002",
        )
        catalog["items"].append(second_item)
        manifest["items"].append(
            {
                "skill_ref": "personal:skill_002",
                "signed_get_url": "https://runtime-test.oss.example.com/skill2.md",
                "content_type": "text/markdown",
            },
        )
    calls = 0

    async def fetch(url: str, max_bytes: int) -> DownloadedPersonalSkill:
        nonlocal calls
        calls += 1
        return DownloadedPersonalSkill(content, url, False)

    registry = PersonalSkillsRegistry.from_payloads(
        catalog,
        manifest,
        fetcher=fetch,
    )
    if expected == "activation limit":
        await registry.activate_personal_skill("personal:skill_001")
        response = await registry.activate_personal_skill("personal:skill_002")
        assert calls == 1
    else:
        response = await registry.activate_personal_skill("personal:skill_001")
        assert calls == 0

    assert expected in _response_text(response).lower()


@pytest.mark.asyncio
async def test_unexpected_fetch_failure_degrades_without_exposing_url(
    monkeypatch,
) -> None:
    monkeypatch.setenv("PERSONAL_SKILLS_ALLOWED_OSS_HOSTS", "oss.example.com")
    catalog, manifest = _payloads()
    secret_url = manifest["items"][0]["signed_get_url"]

    async def fetch(url: str, max_bytes: int) -> DownloadedPersonalSkill:
        raise RuntimeError(f"transport failed for {url}")

    registry = PersonalSkillsRegistry.from_payloads(
        catalog,
        manifest,
        fetcher=fetch,
    )

    response = await registry.activate_personal_skill("personal:skill_001")
    text = _response_text(response)

    assert "download failed" in text.lower()
    assert secret_url not in text


@pytest.mark.asyncio
async def test_registries_are_request_isolated_and_close_redacts_content(
    monkeypatch,
) -> None:
    monkeypatch.setenv("PERSONAL_SKILLS_ALLOWED_OSS_HOSTS", "oss.example.com")
    content = _skill_bytes()
    catalog, manifest = _payloads(content)

    async def fetch(url: str, max_bytes: int) -> DownloadedPersonalSkill:
        return DownloadedPersonalSkill(content, url, False)

    first = PersonalSkillsRegistry.from_payloads(catalog, manifest, fetcher=fetch)
    second = PersonalSkillsRegistry.from_payloads(catalog, manifest, fetcher=fetch)
    activated = await first.activate_personal_skill("personal:skill_001")
    state = {
        "memory": [
            {
                "content": [
                    {
                        "type": "tool_result",
                        "output": [{"type": "text", "text": _response_text(activated)}],
                    },
                ],
            },
        ],
    }

    redacted = first.redact_for_persistence(state)
    first.close()
    closed_response = await first.activate_personal_skill("personal:skill_001")

    assert "汇总本月进展" not in str(redacted)
    assert "not persisted" in str(redacted)
    assert second.activated_skill_refs == ()
    assert "request has ended" in _response_text(closed_response).lower()


def test_protocol_rejects_snapshot_mismatch_and_non_personal_source() -> None:
    catalog, manifest = _payloads()
    manifest["snapshot_id"] = "snapshot_other"
    with pytest.raises(PersonalSkillProtocolError, match="snapshot"):
        PersonalSkillsRegistry.from_payloads(catalog, manifest)

    catalog, manifest = _payloads()
    catalog["items"][0]["source"] = "platform"
    with pytest.raises(PersonalSkillProtocolError, match="source"):
        PersonalSkillsRegistry.from_payloads(catalog, manifest)
