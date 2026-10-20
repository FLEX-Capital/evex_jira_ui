"""Optional password gate for the Streamlit UI."""

import hmac
import secrets

import streamlit as st

from settings import load_settings

_GENERATION_SECRET = secrets.token_bytes(32)


def configured_password() -> str | None:
    """Return the current injected environment password for generation checks."""
    return load_settings().ui_password


def _password_generation(password: str) -> str:
    return hmac.new(_GENERATION_SECRET, password.encode("utf-8"), "sha256").hexdigest()


def _clear_auth_state() -> None:
    for key in (
        "_ui_authed",
        "_ui_auth_generation",
        "_ui_password_input",
        "_ui_auth_error",
    ):
        st.session_state.pop(key, None)


def _authenticate_entered_password() -> None:
    entered = st.session_state.get("_ui_password_input", "")
    password = configured_password()
    _clear_auth_state()
    if password and isinstance(entered, str):
        if hmac.compare_digest(entered.encode("utf-8"), password.encode("utf-8")):
            st.session_state["_ui_authed"] = True
            st.session_state["_ui_auth_generation"] = _password_generation(password)
        else:
            st.session_state["_ui_auth_error"] = True


def _has_current_generation(password: str) -> bool:
    generation = st.session_state.get("_ui_auth_generation")
    return (
        st.session_state.get("_ui_authed") is True
        and isinstance(generation, str)
        and hmac.compare_digest(
            generation.encode("utf-8"), _password_generation(password).encode("ascii")
        )
    )


def require_current_session() -> None:
    """Return expired fragment sessions to the full app's login gate."""
    password = configured_password()
    if not password:
        _clear_auth_state()
    elif not _has_current_generation(password):
        _clear_auth_state()
        st.rerun(scope="app")


def require_password() -> None:
    """Require the current password generation before rendering private UI.

    - Set `UI_PASSWORD` in `.env` or the server environment (e.g. a Render env var)
      to enable.
    - If `UI_PASSWORD` is not set, the UI will be accessible without a password.
    """
    password = configured_password()
    if not password:
        _clear_auth_state()
        return

    generation = st.session_state.get("_ui_auth_generation")
    if _has_current_generation(password):
        st.sidebar.button("🔒 Logout", on_click=_clear_auth_state)
        return
    if st.session_state.get("_ui_authed") or generation is not None:
        _clear_auth_state()

    st.sidebar.markdown("### 🔐 Login")
    st.sidebar.text_input(
        "Passwort",
        type="password",
        key="_ui_password_input",
        on_change=_authenticate_entered_password,
    )
    if st.session_state.get("_ui_auth_error"):
        st.sidebar.error("Falsches Passwort.")

    st.stop()
