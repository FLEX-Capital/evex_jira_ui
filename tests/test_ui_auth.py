"""Require the current password generation before reading cached tickets."""

from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, Mock

import pandas as pd
import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

import data_loading
import ui_auth

AuthApp = tuple[AppTest, dict[str, str | None], Mock]


@pytest.fixture
def auth_app(storage: Path, monkeypatch: pytest.MonkeyPatch) -> AuthApp:
    """Run the real app gate against a mutable secret and isolated empty cache."""
    passwords: dict[str, str | None] = {"value": "password-A"}
    monkeypatch.setattr(ui_auth, "configured_password", lambda: passwords["value"])
    load = Mock(return_value=pd.DataFrame())
    monkeypatch.setattr(data_loading, "load_data", load)
    app = AppTest.from_file("app.py", default_timeout=30).run()
    assert not app.exception
    assert not load.called
    return app, passwords, load


def test_password_rotation_blocks_ticket_reads_until_new_login(
    auth_app: AuthApp,
) -> None:
    """A changed secret revokes existing access before the cache loader runs."""
    app, passwords, load = auth_app
    app.sidebar.text_input[0].set_value("password-A").run()
    assert load.called
    load.reset_mock()
    passwords["value"] = "password-B"
    app.run()
    assert not app.exception
    load.assert_not_called()
    assert not app.sidebar.multiselect
    app.sidebar.text_input[0].set_value("password-B").run()
    assert load.called
    assert len(app.sidebar.multiselect) == 1


def test_legacy_boolean_does_not_authenticate(auth_app: AuthApp) -> None:
    """A session from the old gate cannot bypass generation verification."""
    app, _, load = auth_app
    app.session_state["_ui_authed"] = True
    app.run()
    assert not app.exception
    load.assert_not_called()
    assert not app.sidebar.multiselect
    assert app.sidebar.text_input


def test_wrong_password_never_grants_access(auth_app: AuthApp) -> None:
    """A rejected credential neither grants access nor persists its plaintext."""
    app, _, load = auth_app
    app.sidebar.text_input[0].set_value("wrong-password").run()
    assert not app.exception
    load.assert_not_called()
    assert app.sidebar.error
    state = app.session_state.filtered_state
    assert not state.get("_ui_authed")
    assert not state.get("_ui_auth_generation")
    assert not state.get("_ui_password_input")


def test_logout_clears_authentication_and_input(auth_app: AuthApp) -> None:
    """Logout clears the current credential and revokes reads on the same rerun."""
    app, _, load = auth_app
    app.sidebar.text_input[0].set_value("password-A").run()
    state = app.session_state.filtered_state
    assert state.get("_ui_auth_generation")
    assert "password-A" not in [str(v) for v in state.values()]
    load.reset_mock()
    next(b for b in app.sidebar.button if "Logout" in b.label).click().run()
    assert not app.exception
    load.assert_not_called()
    assert app.sidebar.text_input
    state = app.session_state.filtered_state
    for key in (
        "_ui_authed",
        "_ui_auth_generation",
        "_ui_password_input",
        "_ui_auth_error",
    ):
        assert not state.get(key)


def test_public_mode_does_not_revive_old_session(auth_app: AuthApp) -> None:
    """Removing then restoring a secret cannot restore an older login."""
    app, passwords, load = auth_app
    app.sidebar.text_input[0].set_value("password-A").run()
    passwords["value"] = None
    app.run()
    assert not app.exception
    assert app.sidebar.multiselect
    assert not app.session_state.filtered_state.get("_ui_authed")
    assert not app.session_state.filtered_state.get("_ui_auth_generation")
    load.reset_mock()
    passwords["value"] = "password-A"
    app.run()
    assert not app.exception
    load.assert_not_called()
    assert not app.sidebar.multiselect


def test_rotation_between_callback_and_gate_is_rejected(
    auth_app: AuthApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A credential accepted by a callback is checked again before cache access."""
    app, _, load = auth_app
    calls = 0

    def rotate() -> str:
        nonlocal calls
        calls += 1
        return "password-A" if calls == 1 else "password-B"

    monkeypatch.setattr(ui_auth, "configured_password", rotate)
    app.sidebar.text_input[0].set_value("password-A").run()
    assert not app.exception
    load.assert_not_called()
    assert not app.sidebar.multiselect


def test_authenticated_rerun_reuses_generation(auth_app: AuthApp) -> None:
    """The current generation stays authenticated without keeping the entered secret."""
    app, _, load = auth_app
    app.sidebar.text_input[0].set_value("password-A").run()
    generation = app.session_state["_ui_auth_generation"]
    load.reset_mock()
    app.run()
    assert not app.exception
    assert load.called
    assert app.session_state["_ui_auth_generation"] == generation
    assert not app.session_state.filtered_state.get("_ui_password_input")


@pytest.mark.parametrize("panel", ["status", "history"])
@pytest.mark.parametrize("action", ["timer", "resume", "start"])
def test_fragment_rotation_blocks_status_and_import_actions(
    storage: Path, monkeypatch: pytest.MonkeyPatch, panel: str, action: str
) -> None:
    """An independent fragment must return to the app gate before any job access."""
    import sync_jobs
    import temporary_history_pull as history

    class FullAppRerun(BaseException):
        pass

    state = {
        "_ui_authed": True,
        "_ui_auth_generation": ui_auth._password_generation("password-A"),
    }
    monkeypatch.setattr(st, "session_state", state)
    monkeypatch.setattr(ui_auth, "configured_password", lambda: "password-B")
    jobs = Mock()
    jobs.snapshot.return_value = {
        "state": "partial",
        "request": {"projects": ["SDAX"]},
    }
    monkeypatch.setattr(sync_jobs, "JOB", jobs)
    rerun = Mock(side_effect=FullAppRerun)
    monkeypatch.setattr(st, "rerun", rerun)
    monkeypatch.setattr(st, "container", MagicMock())
    for name in ("caption", "subheader", "info", "warning"):
        monkeypatch.setattr(st, name, Mock())
    monkeypatch.setattr(st, "date_input", Mock(return_value=(date(2025, 1, 1), date(2025, 2, 1))))
    monkeypatch.setattr(st, "multiselect", Mock(return_value=["SDAX"]))
    monkeypatch.setattr(
        st,
        "button",
        lambda label, **kwargs: (
            (action == "resume" and "fortsetzen" in label)
            or (action == "start" and "laden" in label)
        ),
    )
    # Call the actual fragment body, deliberately bypassing app.py's full gate.
    # AppTest.run() itself only simulates whole-app reruns.
    with pytest.raises(FullAppRerun):
        if panel == "status":
            sync_jobs.render_sync_status.__wrapped__("isolated")
        else:
            history.render_history_pull.__wrapped__()
    jobs.snapshot.assert_not_called()
    jobs.resume.assert_not_called()
    jobs.start.assert_not_called()
    rerun.assert_called_once_with(scope="app")
    assert not state.get("_ui_authed")
    assert not state.get("_ui_auth_generation")
