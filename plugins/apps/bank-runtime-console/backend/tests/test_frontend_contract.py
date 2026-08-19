from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
FRONTEND = PLUGIN_ROOT / "ui" / "index.js"


def test_frontend_is_read_only_and_has_no_runtime_credential_inputs() -> None:
    source = FRONTEND.read_text(encoding="utf-8")
    lowered = source.lower()

    for forbidden in (
        "app_token",
        "runtime_password",
        "runtime_user_token",
        "sendmessage",
        "uploadfile",
        "renameconversation",
        "pinconversation",
        "deleteconversation",
    ):
        assert forbidden not in lowered
    assert 'method: "POST"' in source
    assert 'method: "GET"' in source
    assert 'method: "DELETE"' not in source
    assert 'method: "PATCH"' not in source
    assert 'method: "PUT"' not in source


def test_disconnect_clears_only_tab_session_state() -> None:
    source = FRONTEND.read_text(encoding="utf-8")

    assert "sessionStorage" in source
    assert "localStorage" not in source
    assert "sessionStorage.removeItem(IDENTITY_KEY)" in source
    assert "/disconnect" not in source
