from datetime import UTC, date, datetime

import pytest
from streamlit.testing.v1 import AppTest

import data_loading
import sync_jobs


class FakeJobs:
    def __init__(self):
        self.requests = []
        self.state = {"state": "idle", "completion": None}
        self.accept = True

    def snapshot(self):
        return self.state.copy()

    def start(self, request=None, **legacy):
        self.requests.append(request)
        if self.accept:
            self.state = {
                "state": "running",
                "completion": None,
                "progress": "Downloading",
            }
        return self.accept

    def resume(self):
        return self.accept


@pytest.fixture
def ui(tmp_path, monkeypatch):
    import temporary_history_pull as history

    monkeypatch.setattr(data_loading, "DATA_PATH", str(tmp_path / "cache.pkl"))
    jobs = FakeJobs()
    monkeypatch.setattr(sync_jobs, "JOB", jobs)
    if hasattr(history, "JOB"):
        monkeypatch.setattr(history, "JOB", jobs)
    app = AppTest.from_string(
        "from temporary_history_pull import render_history_pull\nrender_history_pull()",
        default_timeout=15,
    ).run()
    return app, jobs


def test_custom_dates_and_desks_are_independent_and_frozen_at_submission(ui):
    app, jobs = ui
    app.date_input(key="temporary_history_dates").set_value(
        (date(2025, 1, 1), date(2025, 3, 17))
    ).run()
    app.multiselect(key="temporary_history_desks").set_value(["SDAX", "SDEU"]).run()
    app.button(key="temporary_history_start").click().run()
    assert not app.exception
    request = jobs.requests[-1]
    assert request.projects == ("SDAX", "SDEU")
    assert request.start == datetime(2024, 12, 31, 23, tzinfo=UTC)
    assert request.end == datetime(2025, 3, 17, 22, 59, 59, 999999, tzinfo=UTC)
    assert not request.repair_sources and not request.publish_raw
    assert app.date_input(key="temporary_history_dates").disabled
    assert app.multiselect(key="temporary_history_desks").disabled
    assert any("Hintergrund" in item.value for item in app.info)


def test_empty_desks_or_incomplete_range_cannot_start_import(ui):
    app, jobs = ui
    app.multiselect(key="temporary_history_desks").set_value([]).run()
    assert app.button(key="temporary_history_start").disabled
    app.multiselect(key="temporary_history_desks").set_value(["SDAX"]).run()
    app.date_input(key="temporary_history_dates").set_value((date(2025, 1, 1),)).run()
    assert app.button(key="temporary_history_start").disabled
    assert not jobs.requests


def test_failed_admission_preserves_selection_and_reports_shared_busy_state(ui):
    app, jobs = ui
    jobs.accept = False
    app.date_input(key="temporary_history_dates").set_value(
        (date(2025, 1, 1), date(2025, 3, 17))
    ).run()
    app.button(key="temporary_history_start").click().run()
    assert not app.exception
    assert app.date_input(key="temporary_history_dates").value == (
        date(2025, 1, 1),
        date(2025, 3, 17),
    )
    assert any("bereits" in item.value for item in app.warning)


def test_partial_category_failure_is_not_shown_as_success_and_can_resume(ui):
    app, jobs = ui
    jobs.state = {
        "state": "partial",
        "request": {"projects": ["SDAX"]},
        "completion": None,
        "report": {
            "complete": False,
            "counts": {"SDAX": 1},
            "rows": 1,
            "categories": {
                "repaired_cells": 4,
                "unassigned_cells": 2,
                "unresolved_cells": 1,
                "failed_objects": {"object": "HTTP 403"},
            },
        },
    }
    app.run()
    assert not app.exception
    assert any("unvollständig" in item.value for item in app.warning)
    assert not app.success
    assert any("1 noch unbekannt" in item.value for item in app.caption)
    assert app.button(key="sync_resume_custom")


def test_midnight_advances_only_the_default_today_end(ui):
    import temporary_history_pull as history

    app, _ = ui
    # Exercise the existing state rule directly with a plain session mapping.
    values = {
        "_history_today": date(2026, 9, 1),
        "temporary_history_dates": (date(2025, 1, 1), date(2026, 9, 1)),
    }
    from unittest.mock import patch

    with patch.object(history.st, "session_state", values):
        history._follow_today(date(2026, 9, 2))
        assert values["temporary_history_dates"][1] == date(2026, 9, 2)
        values["temporary_history_dates"] = (date(2025, 1, 1), date(2025, 3, 17))
        history._follow_today(date(2026, 9, 3))
        assert values["temporary_history_dates"][1] == date(2025, 3, 17)
