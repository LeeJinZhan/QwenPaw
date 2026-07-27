import re
from pathlib import Path


DOCKERFILE = Path(__file__).parents[2] / "deploy" / "Dockerfile"


def _dockerfile_stages() -> tuple[str, str]:
    content = DOCKERFILE.read_text(encoding="utf-8")
    builder_start = content.index("AS console-builder")
    runtime_start = content.index("FROM ${NODE_IMAGE}", builder_start + 1)
    return content[builder_start:runtime_start], content[runtime_start:]


def test_console_builder_uses_configurable_node_heap_of_at_least_4096_mb():
    builder, runtime = _dockerfile_stages()

    build_arg = re.search(
        r'^ARG CONSOLE_BUILD_NODE_OPTIONS="--max-old-space-size=(\d+)"$',
        builder,
        re.MULTILINE,
    )
    assert build_arg is not None
    assert int(build_arg.group(1)) >= 4096
    assert "ENV NODE_OPTIONS=${CONSOLE_BUILD_NODE_OPTIONS}" in builder
    assert builder.index("ENV NODE_OPTIONS=") < builder.index("npm run build")

    assert "NODE_OPTIONS" not in runtime


def test_console_builder_removes_node_modules_after_build_in_the_same_layer():
    builder, _ = _dockerfile_stages()

    build_step = re.search(
        r"^RUN cd /app/console && (?P<command>.+)$",
        builder,
        re.MULTILINE,
    )
    assert build_step is not None
    command = build_step.group("command")
    assert "npm run build" in command
    assert "rm -rf node_modules" in command
    assert command.index("npm run build") < command.index("rm -rf node_modules")
