"""Optional password gate for the Streamlit UI."""

import hmac
import os
from pathlib import Path

import streamlit as st
from dotenv import dotenv_values

# Read directly: the modules that load .env into the environment are imported
# lazily, after the gate has already run.
ENV_FILE = Path(__file__).with_name(".env")


def configured_password():
    """UI_PASSWORD from .env (it wins, like the Jira credentials) or the environment."""
    return dotenv_values(ENV_FILE).get("UI_PASSWORD") or os.getenv("UI_PASSWORD")


def require_password() -> None:
    """
    Simple password gate for the Streamlit UI.

    - Set `UI_PASSWORD` in `.env` or the server environment (e.g. a Render env var)
      to enable.
    - If `UI_PASSWORD` is not set, the UI will be accessible without a password.
    """
    password = configured_password()
    if not password:
        return

    if st.session_state.get("_ui_authed", False):
        if st.sidebar.button("🔒 Logout"):
            st.session_state["_ui_authed"] = False
            st.rerun()
        return

    st.sidebar.markdown("### 🔐 Login")
    entered = st.sidebar.text_input("Passwort", type="password")
    if entered:
        if hmac.compare_digest(entered.encode("utf-8"), password.encode("utf-8")):
            st.session_state["_ui_authed"] = True
            st.rerun()
        else:
            st.sidebar.error("Falsches Passwort.")

    st.stop()
