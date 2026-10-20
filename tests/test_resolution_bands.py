"""Boundary tests for the resolution-time bands."""

import pandas as pd
import pytest

from resolution_bands import (
    BAND_NOT_DONE,
    BAND_OVER_1D,
    BAND_UNDER_1D,
    BAND_UNDER_1H,
    classify_band,
    classify_bands,
)


@pytest.mark.parametrize(
    ("hours", "expected"),
    [
        (0.0, BAND_UNDER_1H),
        (0.5, BAND_UNDER_1H),
        (1.0, BAND_UNDER_1H),  # upper bound is inclusive
        (1.01, BAND_UNDER_1D),
        (9.9, BAND_UNDER_1D),
        (10.0, BAND_UNDER_1D),  # upper bound is inclusive
        (10.01, BAND_OVER_1D),
        (100.0, BAND_OVER_1D),
    ],
)
def test_boundaries(hours, expected):
    assert classify_band(hours, is_done=True) == expected


def test_open_ticket_ignores_hours():
    assert classify_band(0.25, is_done=False) == BAND_NOT_DONE


def test_done_ticket_without_hours_is_unknown():
    assert classify_band(float("nan"), is_done=True) == "Unbekannte Dauer"


def test_none_hours_is_unknown():
    assert classify_band(None, is_done=True) == "Unbekannte Dauer"


def test_custom_working_day_length():
    assert classify_band(9.0, is_done=True, hours_per_working_day=8.0) == BAND_OVER_1D


def test_vectorised_matches_scalar():
    hours = pd.Series([0.5, 1.0, 5.0, 10.0, 20.0, float("nan")])
    done = pd.Series([True, True, True, True, True, True])
    expected = [classify_band(h, d) for h, d in zip(hours, done, strict=False)]
    assert list(classify_bands(hours, done)) == expected


def test_vectorised_honours_is_done():
    hours = pd.Series([0.5, 0.5])
    done = pd.Series([True, False])
    assert list(classify_bands(hours, done)) == [BAND_UNDER_1H, BAND_NOT_DONE]


def test_vectorised_does_not_mutate_inputs():
    hours = pd.Series([0.5, 20.0])
    done = pd.Series([True, False])
    hours_before, done_before = hours.copy(), done.copy()
    classify_bands(hours, done)
    pd.testing.assert_series_equal(hours, hours_before)
    pd.testing.assert_series_equal(done, done_before)
