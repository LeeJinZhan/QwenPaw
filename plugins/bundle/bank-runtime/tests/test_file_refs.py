from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile

import pytest

from bank_runtime.sandbox.cache import PreparedSandboxFile
from bank_runtime.sandbox.file_refs import FileRefError, FileRefRegistry


def _prepared(root: Path, task_id: str, content: bytes = b"runtime attachment") -> PreparedSandboxFile:
    task_root = root / task_id
    task_root.mkdir(mode=0o700)
    path = task_root / "file_001.pdf"
    path.write_bytes(content)
    path.chmod(0o600)
    return PreparedSandboxFile(
        file_id="file_001",
        local_path=path,
        content_type="application/pdf",
        size_bytes=len(content),
        original_name="年度报告.pdf",
        expires_at="",
        task_id=task_id,
    )


def test_file_ref_resolves_only_for_the_bound_task() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        now = datetime(2026, 8, 27, tzinfo=timezone.utc)
        registry = FileRefRegistry(
            root=root,
            process_start_key=b"k" * 32,
            clock=lambda: now,
        )
        token = registry.issue(
            _prepared(root, "task_001"),
            expires_at=now + timedelta(minutes=5),
        )

        resolved = registry.resolve(token, expected_task_id="task_001")

        assert token.startswith("fr1_")
        assert resolved.task_id == "task_001"
        assert resolved.file_id == "file_001"
        assert resolved.extension == ".pdf"
        assert resolved.path == (root / "task_001" / "file_001.pdf").resolve()
        assert "年度报告" not in token

        with pytest.raises(FileRefError) as mismatch:
            registry.resolve(token, expected_task_id="task_002")
        assert mismatch.value.code == "FILE_ACCESS_DENIED"


def test_file_ref_rejects_tampering_expiry_replacement_and_revocation() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        current = [datetime(2026, 8, 27, tzinfo=timezone.utc)]
        registry = FileRefRegistry(
            root=root,
            process_start_key=b"s" * 32,
            clock=lambda: current[0],
        )
        prepared = _prepared(root, "task_002")
        token = registry.issue(
            prepared,
            expires_at=current[0] + timedelta(minutes=1),
        )

        replacement = "0" if token[-1] != "0" else "1"
        with pytest.raises(FileRefError) as tampered:
            registry.resolve(f"{token[:-1]}{replacement}", expected_task_id="task_002")
        assert tampered.value.code == "FILE_REF_INVALID"

        prepared.local_path.write_bytes(b"runtime attachmenT")
        with pytest.raises(FileRefError) as replaced:
            registry.resolve(token, expected_task_id="task_002")
        assert replaced.value.code == "FILE_REF_INVALID"

        prepared.local_path.write_bytes(b"runtime attachment")
        registry.revoke_task("task_002")
        with pytest.raises(FileRefError) as revoked:
            registry.resolve(token, expected_task_id="task_002")
        assert revoked.value.code == "FILE_REF_EXPIRED"

        fresh = registry.issue(
            prepared,
            expires_at=current[0] + timedelta(minutes=1),
        )
        current[0] += timedelta(minutes=2)
        assert registry.purge_expired() == 1
        with pytest.raises(FileRefError) as expired:
            registry.resolve(fresh, expected_task_id="task_002")
        assert expired.value.code == "FILE_REF_EXPIRED"


def test_file_ref_issue_rejects_files_outside_the_task_root() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory) / "root"
        root.mkdir()
        outside = Path(directory) / "outside.pdf"
        outside.write_bytes(b"outside")
        registry = FileRefRegistry(root=root, process_start_key=b"x" * 32)
        prepared = PreparedSandboxFile(
            file_id="file_003",
            local_path=outside,
            content_type="application/pdf",
            size_bytes=7,
            original_name="outside.pdf",
            expires_at="",
            task_id="task_003",
        )

        with pytest.raises(FileRefError) as denied:
            registry.issue(
                prepared,
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=1),
            )
        assert denied.value.code == "FILE_ACCESS_DENIED"
