from __future__ import annotations

import inspect
from importlib.metadata import version
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 compatibility
    import tomli as tomllib


QWENPAW_ROOT = Path(__file__).resolve().parents[3]


def test_installed_acp_dependency_uses_supported_minor_version() -> None:
    installed_version = version("agent-client-protocol")

    assert installed_version.split(".")[:2] == ["0", "10"]


def test_pyproject_pins_supported_acp_dependency() -> None:
    with (QWENPAW_ROOT / "pyproject.toml").open("rb") as pyproject_file:
        pyproject = tomllib.load(pyproject_file)

    dependencies = pyproject["project"]["dependencies"]
    assert "agent-client-protocol==0.10.1" in dependencies


def test_acp_exports_set_session_model_response() -> None:
    from acp import SetSessionModelResponse

    assert SetSessionModelResponse is not None


def test_qwenpaw_acp_agent_declares_set_session_model_return_type() -> None:
    from qwenpaw.agents.acp.server import QwenPawACPAgent

    return_annotation = inspect.signature(
        QwenPawACPAgent.set_session_model,
    ).return_annotation

    assert return_annotation is not inspect.Signature.empty
    assert "SetSessionModelResponse" in str(return_annotation)
