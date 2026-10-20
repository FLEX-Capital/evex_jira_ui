"""View extraction preserves widget state, gates, reloads and interactive boundaries."""

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import FrozenInstanceError, fields
from datetime import date
from typing import Any
from unittest.mock import patch

import pandas as pd
import pytest
import test_ui
from streamlit.testing.v1 import AppTest


@pytest.fixture(autouse=True)
def preserve_process_entrypoint() -> Iterator[None]:
    """Restore AppTest's replaced __main__ before later multiprocessing spawn tests."""
    original = sys.modules["__main__"]
    try:
        yield
    finally:
        sys.modules["__main__"] = original


@contextmanager
def _app(frame: pd.DataFrame) -> Iterator[AppTest]:
    with (
        patch("data_loading.load_data", return_value=frame),
        patch("interactive.render_interactive"),
    ):
        app = AppTest.from_file("app.py", default_timeout=30).run()
        app.sidebar.date_input[0].set_value((date(2026, 9, 1), date(2026, 9, 10))).run()
        assert not app.exception
        yield app


def test_context_is_explicit_and_styles_are_scoped() -> None:
    """Context carries required page inputs and CSS avoids generated/global selectors."""
    from dashboard_views import DashboardContext
    from settings import load_settings
    from styles import CUSTOM_CSS

    assert {"frame", "full_frame", "revision", "settings", "x_axis", "x_label"} <= {
        f.name for f in fields(DashboardContext)
    }
    assert "css-" not in CUSTOM_CSS and "p, div, span, label" not in CUSTOM_CSS
    assert ".st-key-dashboard" in CUSTOM_CSS
    context = DashboardContext(pd.DataFrame(), pd.DataFrame(), None, load_settings(), "day", "Day")
    with pytest.raises(FrozenInstanceError):
        setattr(context, "x_axis", "other")


def test_views_keep_independent_widget_state(public_ui: None) -> None:
    """Each existing mode key and country cohort choice persists independently."""
    import dashboard_views

    assert callable(dashboard_views.render_dashboard)
    with _app(test_ui.CountryRefreshUITests()._frame()) as app:
        keys = [
            "toggle_categories",
            "toggle_sources",
            "toggle_ursprung",
            "toggle_countries",
            "toggle_resolution_time",
        ]
        for key in keys:
            app.radio(key=key).set_value("Relativ (%)").run()
            assert app.radio(key=key).value == "Relativ (%)"
            for other in keys[keys.index(key) + 1 :]:
                assert app.radio(key=other).value == "Absolute Zahlen"
        app.checkbox(key="land_include_open").uncheck().run()
        assert all(app.radio(key=key).value == "Relativ (%)" for key in keys)
        assert not app.checkbox(key="land_include_open").value and len(app.tabs) == 13


@pytest.mark.parametrize("empty_kind", ["cache", "filtered", "company"])
def test_empty_cache_filtered_empty_and_no_company_keep_import_controls(
    public_ui: None,
    empty_kind: str,
) -> None:
    """All empty branches retain independent custom dates/desks and all-desk defaults."""
    import dashboard_views

    assert callable(dashboard_views.render_dashboard)
    frame = pd.DataFrame() if empty_kind == "cache" else test_ui.CountryRefreshUITests()._frame()
    with _app(frame) as app:
        if empty_kind == "filtered":
            app.sidebar.date_input[0].set_value((date(2024, 1, 1), date(2024, 1, 2))).run()
        elif empty_kind == "company":
            app.sidebar.multiselect[0].set_value([]).run()
        assert not app.exception
        expected = {
            "cache": "Keine Jira-Daten",
            "filtered": "Keine Daten",
            "company": "Firma",
        }[empty_kind]
        assert any(expected in info.value for info in app.info)
        assert app.multiselect(key="temporary_history_desks").value == [
            "SDIPR",
            "SDAX",
            "SDEU",
        ]
        app.date_input(key="temporary_history_dates").set_value(
            (date(2020, 1, 1), date(2020, 1, 2))
        ).run()
        app.multiselect(key="temporary_history_desks").set_value(["SDAX"]).run()
        assert app.date_input(key="temporary_history_dates").value == (
            date(2020, 1, 1),
            date(2020, 1, 2),
        )
        assert app.multiselect(key="temporary_history_desks").value == ["SDAX"]


def test_full_app_and_fragment_rotation_gate_before_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Importing view modules cannot read a cache before the current page gate."""
    import dashboard_views

    assert callable(dashboard_views.render_dashboard)
    monkeypatch.setenv("UI_PASSWORD", "A")
    with patch("data_loading.load_data", return_value=pd.DataFrame()) as read:
        app = AppTest.from_file("app.py", default_timeout=30).run()
        assert not read.called
        app.sidebar.text_input[0].set_value("A").run()
        assert read.called
        read.reset_mock()
        monkeypatch.setenv("UI_PASSWORD", "B")
        app.run()
        assert not read.called and app.sidebar.text_input and not app.exception


def test_background_revision_reload_preserves_selection(public_ui: None) -> None:
    """A replaced cache is reloaded while page/date/company/mode selections stay selected."""
    import dashboard_views

    assert callable(dashboard_views.render_dashboard)
    frame = test_ui.CountryRefreshUITests()._frame()
    current = {"frame": frame, "revision": (1, 1, 1)}
    with (
        patch("data_loading.load_data", side_effect=lambda **kw: current["frame"]),
        patch(
            "data_loading.cache_revision",
            side_effect=lambda *a, **kw: current["revision"],
        ),
        patch("interactive.render_interactive"),
    ):
        app = AppTest.from_file("app.py", default_timeout=30).run()
        app.sidebar.date_input[0].set_value((date(2026, 9, 1), date(2026, 9, 10))).run()
        app.sidebar.multiselect[0].set_value(["Euronet"]).run()
        app.radio(key="toggle_categories").set_value("Relativ (%)").run()
        current["frame"] = frame.assign(key=["SDEU-NEW", "SDEU-OLD"])
        current["revision"] = (2, 2, 2)
        app.run()
        assert not app.exception and "SDEU-NEW" in app.dataframe[0].value.key.tolist()
        assert app.sidebar.multiselect[0].value == ["Euronet"]
        assert app.sidebar.date_input[0].value == (date(2026, 9, 1), date(2026, 9, 10))
        assert app.radio(key="toggle_categories").value == "Relativ (%)"


def test_interactive_filters_collection_columns_without_losing_lock(
    public_ui: None,
) -> None:
    """Views exclude collection columns and the actual renderer still owns its global lock."""
    import dashboard_views
    import interactive

    assert callable(dashboard_views.render_interactive_view)
    frame = test_ui.CountryRefreshUITests()._frame()
    for column, value in [("lists", [1]), ("dicts", {"a": 1}), ("sets", {1})]:
        frame[column] = [value] * len(frame)
    with (
        patch("data_loading.load_data", return_value=frame),
        patch("interactive.render_interactive") as render,
    ):
        app = AppTest.from_file("app.py", default_timeout=30).run()
        app.sidebar.date_input[0].set_value((date(2026, 9, 1), date(2026, 9, 10))).run()
        assert not app.exception
        supplied = render.call_args.args[0]
        assert "key" in supplied and not {"lists", "dicts", "sets"}.intersection(supplied.columns)
    held = []

    def check_lock(*args: Any, **kwargs: Any) -> Any:
        acquired = interactive._renderer_lock.acquire(blocking=False)
        if acquired:
            interactive._renderer_lock.release()
        held.append(not acquired)
        return type("Renderer", (), {"explorer": lambda self: None})()

    with patch.object(interactive, "StreamlitRenderer", side_effect=check_lock):
        interactive.render_interactive(pd.DataFrame({"scalar": [1]}))
    assert held == [True]
