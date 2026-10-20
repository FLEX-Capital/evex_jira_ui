"""Independent-review regressions for analytics boundaries and recovery."""

from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

import pandas as pd
import plotly.graph_objects as go
import pytest
from streamlit.testing.v1 import AppTest
from test_sync_service import raw, request
from test_transformation import issue

import desk_sync
import sync_checkpoints
from data_transformation import load_issues_Euronet, upsert_jira_data
from plotting import create_resolution_time_charts


def test_mixed_legacy_refresh_preserves_completed_cohorts(
    storage: Path, public_ui: None
) -> None:
    """Real upsert of new rows cannot demote untouched legacy completed rows."""
    old = load_issues_Euronet([issue(main_id=None, sub_id=None)])
    old = old.drop(
        columns=["completion_state", "status_category_key", "duration_quality"]
    )
    new = load_issues_Euronet([issue(key="SDEU-2", main_id=None, sub_id=None)])
    combined = upsert_jira_data(old, new)
    before = combined.copy(deep=True)
    for fig in create_resolution_time_charts(combined):
        assert fig.layout.meta["completed"] == 2
        assert fig.layout.meta["unknown_completion_excluded"] == 0
        assert sum(int(n) for trace in fig.data for n in trace.y) == 2
    seen: list[go.Figure] = []
    with (
        patch("data_loading.load_data", return_value=combined),
        patch("interactive.render_interactive"),
        patch(
            "streamlit.plotly_chart",
            side_effect=lambda figure, **kw: seen.append(figure),
        ),
    ):
        app = AppTest.from_file("app.py", default_timeout=30).run()
        app.sidebar.date_input[0].set_value((date(2026, 9, 1), date(2026, 9, 10))).run()
        app.checkbox(key="land_include_open").uncheck().run()
    assert not app.exception
    country = [figure for figure in seen if figure.layout.xaxis.title.text == "Land"][
        -1
    ]
    assert (
        sum(int(n) for trace in country.data if trace.type == "bar" for n in trace.y)
        == 2
    )
    assert any("Abgeschlossen: 2" in c.value for c in app.caption)
    pd.testing.assert_frame_equal(combined, before)


def test_resume_retries_window_with_rejected_records(
    storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rejected coverage is retried, then can recover to a complete report."""
    good, bad = raw(), raw(number=2)
    bad["fields"]["created"] = "invalid"
    fetched = Mock(return_value=[bad, good])
    monkeypatch.setattr(sync_checkpoints, "fetch_jira_issues", fetched)
    first = desk_sync.run_sync(request(projects=["SDAX"]))
    assert first.invalid_rows == {"SDAX": 1} and not first.complete
    fetched.reset_mock()
    still_bad = desk_sync.run_sync(request(projects=["SDAX"], resume=True))
    assert still_bad.invalid_rows == {"SDAX": 1} and not still_bad.complete
    assert fetched.call_args.args[:2] == (request().start, request().end)
    fetched.reset_mock()
    fetched.return_value = [good, raw(number=2)]
    recovered = desk_sync.run_sync(request(projects=["SDAX"], resume=True))
    assert fetched.call_args.args[:2] == (request().start, request().end)
    assert recovered.complete and recovered.invalid_rows == {}
    assert set(pd.read_pickle(storage).key) == {"SDAX-1", "SDAX-2"}


def test_loader_preserves_valid_mixed_timestamp_formats(storage: Path) -> None:
    """No preliminary conversion may discard valid formats before enrichment."""
    first, second = issue(main_id=None, sub_id=None), issue(
        key="SDEU-2", main_id=None, sub_id=None
    )
    second["fields"]["created"] = "2026-09-01T08:00:00Z"
    second["fields"]["updated"] = "2026-09-01T14:00:00+02:00"
    second["fields"]["customfield_10010"]["currentStatus"]["statusDate"][
        "jira"
    ] = "2026-09-01T12:00:00Z"
    result = load_issues_Euronet([first, second])
    assert result.created.notna().all() and result.updated.notna().all()
    assert result.currentstatus_date.notna().all()
    assert result.time_to_resolution_h.tolist() == [4.0, 4.0]
    assert result.duration_quality.tolist() == ["valid", "valid"]
