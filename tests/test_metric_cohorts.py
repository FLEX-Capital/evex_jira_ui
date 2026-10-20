"""Duration and dashboard views must reconcile one canonical completion cohort."""

from datetime import date, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pandas as pd
import plotly.graph_objects as go
from streamlit.testing.v1 import AppTest
from test_transformation import issue

from data_transformation import enrich_jira_time_metrics, load_issues_Euronet
from plotting import create_resolution_time_charts


def _frame() -> pd.DataFrame:
    start = pd.Timestamp("2026-09-01T08:00:00Z")
    rows = []
    for index, hours in enumerate([0, 504, 504.1, 940, None, -1, 1, 1]):
        rows.append(
            {
                "key": f"SDAX-{index}",
                "created": start,
                "currentstatus_date": (
                    None if hours is None else start + timedelta(hours=hours)
                ),
                "status_category_key": (
                    "done" if index < 6 else "new" if index == 6 else "unsupported"
                ),
                "currentstatus_name": (
                    "Fertig" if index >= 6 else "Localized different label"
                ),
                "firma": None,
                "request_type": None,
                "source": None,
                "Hauptkategorie": None,
                "Land": None,
            }
        )
    return enrich_jira_time_metrics(pd.DataFrame(rows))


def _bar_total(fig: go.Figure) -> int:
    return sum(
        int(value) for trace in fig.data if trace.type == "bar" for value in trace.y
    )


def test_duration_figures_account_for_504_overflow_and_unknown() -> None:
    """Every done row belongs to a finite, overflow or unknown duration bin."""
    from reporting_policy import ELAPSED_ORDER

    assert ELAPSED_ORDER[-2:] == ("> 504", "Unbekannte Dauer")
    assert "336–504" in ELAPSED_ORDER
    frame = _frame()
    assert frame.time_to_resolution_bin.tolist() == [
        "0–1",
        "336–504",
        "> 504",
        "> 504",
        "Unbekannte Dauer",
        "Unbekannte Dauer",
        "Nicht abgeschlossen",
        "Unbekannter Abschlussstatus",
    ]
    for fig in create_resolution_time_charts(frame):
        assert _bar_total(fig) == 6
        assert fig.layout.meta == {
            "completed": 6,
            "open_excluded": 1,
            "unknown_completion_excluded": 1,
        }
        assert any("6" in a.text and "1" in a.text for a in fig.layout.annotations)
        assert tuple(fig.layout.xaxis.categoryarray) == ELAPSED_ORDER


def test_missing_group_labels_do_not_drop_duration_rows() -> None:
    """Missing category/country labels remain visible in duration aggregations."""
    import plotting

    frame = _frame()
    for fig in create_resolution_time_charts(frame):
        assert _bar_total(fig) == 6
    for group in ["firma", "Hauptkategorie", "Land"]:
        with (
            patch.object(plotting.st, "radio", return_value=plotting.MODE_ABS),
            patch.object(plotting.st, "plotly_chart") as rendered,
        ):
            plotting.create_toggle_chart(frame.iloc[:6], group, "resolution_band")
        assert _bar_total(rendered.call_args.args[0]) == 6
        assert "Unbekannt" in rendered.call_args.args[0].data[0].x
    for fig in create_resolution_time_charts(frame.iloc[:0]):
        assert _bar_total(fig) == 0
        assert fig.layout.yaxis.range[1] >= 1


def test_status_customer_country_views_share_completion(
    storage: Path, public_ui: None
) -> None:
    """Stable status keys agree across views, including an old in-memory frame."""
    raws = []
    for index, key in enumerate(["done", "new", "unsupported"], 1):
        value = issue(key=f"SDEU-{index}", main_id=None, sub_id=None)
        value["fields"]["status"]["statusCategory"] = {"key": key, "name": "Fertig"}
        value["fields"]["customfield_10010"]["currentStatus"]["status"] = (
            "Offen" if key == "done" else "Fertig"
        )
        raws.append(value)
    frame = load_issues_Euronet(raws).drop(columns="completion_state")
    seen: list[tuple[object, pd.DataFrame]] = []

    def capture_chart(df: pd.DataFrame, **kwargs: object) -> None:
        seen.append((kwargs.get("toggle_key"), df.copy()))

    import plotly.express as px

    original_bar = px.bar
    bars: list[tuple[object, pd.DataFrame]] = []

    def capture_bar(data_frame: pd.DataFrame, **kwargs: Any) -> go.Figure:
        bars.append((kwargs.get("x"), data_frame.copy()))
        return original_bar(data_frame, **kwargs)

    with (
        patch("data_loading.load_data", return_value=frame),
        patch("interactive.render_interactive"),
        patch("plotting.create_toggle_chart", side_effect=capture_chart),
        patch.object(px, "bar", side_effect=capture_bar),
    ):
        app = AppTest.from_file("app.py", default_timeout=30).run()
        app.sidebar.date_input[0].set_value((date(2026, 9, 1), date(2026, 9, 10))).run()
        assert not app.exception
        app.checkbox(key="land_include_open").uncheck().run()
        assert not app.exception
    country = [df for key, df in seen if key == "toggle_countries"][-1]
    assert country.key.tolist() == ["SDEU-1"]
    assert country.completion_state.tolist() == ["done"]
    status = [df for key, df in bars if key == "status_category"][-1]
    customer = [df for key, df in bars if key == "zentrale"][-1]
    assert int(status.key.sum()) == 1
    assert int(customer.Anzahl.sum()) == 1
    assert any("Unbekannter Abschlussstatus: 1" in c.value for c in app.caption)
