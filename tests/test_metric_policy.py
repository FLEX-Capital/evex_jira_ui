"""Completion authority and time-quality regressions for Jira metrics."""

from datetime import time
from pathlib import Path

import pandas as pd
import pytest

from data_transformation import enrich_jira_time_metrics


@pytest.mark.parametrize(
    ("key", "status", "expected"),
    [
        ("done", "Wartend", "done"),
        ("new", "Fertig", "open"),
        ("indeterminate", "Fertig", "open"),
        (None, "Fertig", "done"),
        (pd.NA, "Fertig", "done"),
        (None, "Wartend", "open"),
        (None, None, "unknown"),
        ("unexpected", "Fertig", "unknown"),
    ],
)
def test_status_key_overrides_localized_labels(
    key: object, status: object, expected: str
) -> None:
    """Stable category identity wins over localized or conflicting labels."""
    from reporting_policy import completion_state

    assert completion_state(key, status) == expected


@pytest.mark.parametrize("bad", [None, pd.NA, pd.NaT, "not-a-date"])
@pytest.mark.parametrize("column", ["created", "currentstatus_date"])
def test_nullable_or_reversed_time_is_unknown(bad: object, column: str) -> None:
    """Bad resolution endpoints cannot fabricate fast or zero duration."""
    row: dict[str, object] = dict(
        created="2026-09-01T08:00:00Z",
        updated=None,
        currentstatus_date="2026-09-01T12:00:00Z",
        currentstatus_name="Fertig",
    )
    row[column] = bad
    result = enrich_jira_time_metrics(pd.DataFrame([row])).iloc[0]
    assert pd.isna(result.time_to_resolution_h)
    assert pd.isna(result.time_to_resolution_biz_hours)
    assert pd.isna(result.business_days_created_to_updated)
    assert result.duration_quality == "missing"
    assert result.resolution_band == "Unbekannte Dauer"
    assert result.resolution == "Unbekannte Dauer"


def test_reversed_and_equal_intervals_have_different_quality() -> None:
    """Reversed intervals are unknown; valid equal endpoints really are zero."""
    start = "2026-09-01T08:00:00Z"
    frame = pd.DataFrame(
        [
            dict(created=start, currentstatus_date=end, currentstatus_name="Fertig")
            for end in ["2026-09-01T07:00:00Z", start]
        ]
    )
    result = enrich_jira_time_metrics(frame)
    assert result.duration_quality.tolist() == ["reversed", "valid"]
    assert pd.isna(result.loc[0, "time_to_resolution_biz_hours"])
    assert result.loc[1, "time_to_resolution_biz_hours"] == 0
    assert result.resolution_band.tolist() == ["Unbekannte Dauer", "≤ 1 Stunde"]


@pytest.mark.parametrize(
    ("created", "resolved", "calendar_days", "hours"),
    [
        ("2026-03-29T00:30:00+01:00", "2026-03-29T23:30:00+02:00", 0, 0),
        ("2026-10-25T00:30:00+02:00", "2026-10-26T09:00:00+01:00", 1, 1),
        ("2026-01-06T08:00:00+01:00", "2026-01-06T18:00:00+01:00", 0, 0),
        ("2026-09-01T08:00:00+02:00", "2026-09-01T18:00:00+02:00", 0, 10),
    ],
)
def test_calendar_and_business_units_remain_distinct(
    created: str, resolved: str, calendar_days: int, hours: int
) -> None:
    """Calendar dates, Berlin DST and BW working hours retain independent units."""
    result = enrich_jira_time_metrics(
        pd.DataFrame(
            [
                dict(
                    created=created,
                    currentstatus_date=resolved,
                    currentstatus_name="Fertig",
                )
            ]
        )
    ).iloc[0]
    assert result.time_to_resolution_calendar_days == calendar_days
    assert result.time_to_resolution_biz_hours == hours


def test_effective_business_window_sets_day_band() -> None:
    """An explicit eight-hour window does not retain a hidden ten-hour threshold."""
    result = enrich_jira_time_metrics(
        pd.DataFrame(
            [
                dict(
                    created="2026-09-01T08:00:00+02:00",
                    currentstatus_date="2026-09-02T09:00:00+02:00",
                    currentstatus_name="Fertig",
                )
            ]
        ),
        business_start=time(8),
        business_end=time(16),
    ).iloc[0]
    assert result.time_to_resolution_biz_hours == 9
    assert result.time_to_resolution_biz_days == 1.125
    assert result.resolution_band == "> 1 Arbeitstag"


def test_scalar_vector_bands_agree_for_nullable_completion() -> None:
    """Unknown completion differs from known-open and completed unknown duration."""
    from resolution_bands import classify_band, classify_bands

    hours = pd.Series([None, -1, 0.5, 0.5, 0.5])
    done = pd.Series([True, True, True, False, pd.NA], dtype="boolean")
    before = done.copy()
    expected = [
        "Unbekannte Dauer",
        "Unbekannte Dauer",
        "≤ 1 Stunde",
        "Nicht abgeschlossen",
        "Unbekannter Abschlussstatus",
    ]
    assert classify_bands(hours, done).tolist() == expected
    assert [classify_band(h, d) for h, d in zip(hours, done, strict=True)] == expected
    pd.testing.assert_series_equal(done, before)


def test_extracted_category_key_controls_completion(storage: Path) -> None:
    """Extraction retains the stable Jira category key all the way to metrics."""
    from test_sync_service import raw

    from data_transformation import load_project_issues

    issue = raw()
    issue["fields"]["status"]["statusCategory"].update(key="new", name="Fertig")
    row = load_project_issues("SDAX", [issue]).iloc[0]
    assert row.status_category_key == "new"
    assert row.completion_state == "open"
    assert not row.is_done
