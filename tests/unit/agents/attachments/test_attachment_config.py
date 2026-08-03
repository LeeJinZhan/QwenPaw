from pathlib import Path

from qwenpaw.config.config import AgentsRunningConfig


def test_runtime_attachment_inline_budgets_have_safe_defaults() -> None:
    config = AgentsRunningConfig()

    assert config.runtime_attachment_inline_file_max_chars > 0
    assert config.runtime_attachment_inline_task_max_chars >= config.runtime_attachment_inline_file_max_chars


def test_runtime_attachment_inline_task_budget_cannot_be_smaller_than_file_budget() -> None:
    try:
        AgentsRunningConfig(
            runtime_attachment_inline_file_max_chars=1000,
            runtime_attachment_inline_task_max_chars=999,
        )
    except Exception as error:
        assert "runtime_attachment_inline" in str(error)
    else:  # pragma: no cover
        raise AssertionError("invalid attachment budgets must be rejected")


def test_worker_runtime_dependencies_include_pdf_reader() -> None:
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10 compatibility
        import tomli as tomllib

    project_root = Path(__file__).resolve().parents[4]
    with (project_root / "pyproject.toml").open("rb") as project_file:
        dependencies = tomllib.load(project_file)["project"]["dependencies"]

    assert any(dependency.startswith("pypdf") for dependency in dependencies)
