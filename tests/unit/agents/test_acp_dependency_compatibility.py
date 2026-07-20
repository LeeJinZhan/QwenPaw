from __future__ import annotations

from importlib.metadata import version
from pathlib import Path
from typing import get_type_hints

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 compatibility
    import tomli as tomllib


QWENPAW_ROOT = Path(__file__).resolve().parents[3]


def _project_acp_dependency() -> str:
    with (QWENPAW_ROOT / "pyproject.toml").open("rb") as pyproject_file:
        pyproject = tomllib.load(pyproject_file)

    dependencies = pyproject["project"]["dependencies"]
    return next(
        dependency
        for dependency in dependencies
        if dependency.startswith("agent-client-protocol")
    )


def test_installed_acp_dependency_matches_project_pin() -> None:
    dependency = _project_acp_dependency()
    _, separator, pinned_version = dependency.partition("==")

    assert separator == "=="
    assert version("agent-client-protocol") == pinned_version


def test_pyproject_pins_supported_acp_dependency() -> None:
    assert _project_acp_dependency() == "agent-client-protocol==0.10.1"


def test_acp_exports_set_session_model_response() -> None:
    from acp import SetSessionModelResponse

    assert SetSessionModelResponse is not None


def test_qwenpaw_acp_agent_declares_set_session_model_return_type() -> None:
    from acp import Agent, SetSessionModelResponse
    from qwenpaw.agents.acp.server import QwenPawACPAgent

    acp_return_type = get_type_hints(Agent.set_session_model)["return"]
    qwenpaw_return_type = get_type_hints(
        QwenPawACPAgent.set_session_model,
    )["return"]

    assert qwenpaw_return_type == acp_return_type
    assert qwenpaw_return_type == SetSessionModelResponse | None
