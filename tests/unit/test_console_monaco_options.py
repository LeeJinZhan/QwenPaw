import json
import re
from pathlib import Path


CONSOLE_ROOT = Path(__file__).resolve().parents[2] / "console"
TABBED_EDITOR_SOURCE = (
    CONSOLE_ROOT
    / "src"
    / "pages"
    / "Coding"
    / "TabbedEditor.tsx"
)
PACKAGE_LOCK = CONSOLE_ROOT / "package-lock.json"


def test_tabbed_editor_hover_matches_locked_monaco_api() -> None:
    package_lock = json.loads(PACKAGE_LOCK.read_text(encoding="utf-8"))
    source = TABBED_EDITOR_SOURCE.read_text(encoding="utf-8")

    assert (
        package_lock["packages"]["node_modules/monaco-editor"]["version"]
        == "0.55.1"
    )
    assert re.search(r"hover:\s*\{\s*enabled:\s*true\s*\}", source)
    assert not re.search(
        r'hover:\s*\{\s*enabled:\s*"(?:on|off|onKeyboardModifier)"\s*\}',
        source,
    )
