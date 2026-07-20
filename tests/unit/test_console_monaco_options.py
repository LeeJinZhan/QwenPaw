from pathlib import Path
import re


TABBED_EDITOR_SOURCE = (
    Path(__file__).resolve().parents[2]
    / "console"
    / "src"
    / "pages"
    / "Coding"
    / "TabbedEditor.tsx"
)


def test_tabbed_editor_uses_supported_monaco_hover_mode() -> None:
    source = TABBED_EDITOR_SOURCE.read_text(encoding="utf-8")

    assert re.search(r'hover:\s*\{\s*enabled:\s*"on"\s*\}', source)
    assert not re.search(
        r"hover:\s*\{\s*enabled:\s*(?:true|false)\s*\}", source
    )
