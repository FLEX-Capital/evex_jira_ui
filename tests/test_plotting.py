"""Stack-order tests for create_toggle_chart.

The resolution bands are a colour ramp (green -> yellow -> red -> grey), so
they only read correctly when stacked in BAND_ORDER. Left to groupby, the
order is alphabetical by label, which interleaves the ramp.
"""

import pandas as pd
import plotly.graph_objects as go
import pytest

import plotting
from resolution_bands import BAND_ORDER


@pytest.fixture
def captured(monkeypatch):
    """Capture actual compatibility-adapter figures without rendering a UI."""
    seen = []
    monkeypatch.setattr(plotting.st, "radio", lambda *a, **k: plotting.MODE_ABS)
    monkeypatch.setattr(plotting.st, "plotly_chart", lambda figure, **kw: seen.append(figure))
    return seen


def _groups(captured: list[go.Figure]) -> list[str]:
    return [trace.name for trace in captured[-1].data if trace.type == "bar"]


def _frame():
    # Deliberately built so alphabetical order != BAND_ORDER.
    rows = []
    for country in ("Deutschland", "Österreich"):
        for band in reversed(BAND_ORDER):
            rows.append({"Land": country, "resolution_band": band, "key": f"{band}-1"})
    return pd.DataFrame(rows)


def test_group_order_is_honoured(captured):
    """Present compatibility groups retain their explicit meaningful order."""
    plotting.create_toggle_chart(
        _frame(),
        x_col="Land",
        group_col="resolution_band",
        group_order=BAND_ORDER,
        toggle_key="t1",
    )
    assert _groups(captured) == BAND_ORDER


def test_without_group_order_the_stack_is_not_the_ramp(captured):
    """Documents why group_order exists - remove it and the ramp interleaves."""
    plotting.create_toggle_chart(
        _frame(), x_col="Land", group_col="resolution_band", toggle_key="t2"
    )
    assert _groups(captured) != BAND_ORDER


def test_labels_absent_from_the_data_are_not_charted(captured):
    """Compatibility legends keep omitting bands absent from the supplied data."""
    df = _frame()
    df = df[df["resolution_band"] != BAND_ORDER[1]]

    plotting.create_toggle_chart(
        df,
        x_col="Land",
        group_col="resolution_band",
        group_order=BAND_ORDER,
        toggle_key="t3",
    )

    expected = [band for band in BAND_ORDER if band != BAND_ORDER[1]]
    assert _groups(captured) == expected


def test_unexpected_label_is_appended_not_dropped(captured):
    """An unknown band must still be charted, or its tickets vanish silently."""
    df = _frame()
    df.loc[0, "resolution_band"] = "Neue Kategorie"

    plotting.create_toggle_chart(
        df,
        x_col="Land",
        group_col="resolution_band",
        group_order=BAND_ORDER,
        toggle_key="t4",
    )

    order = _groups(captured)
    assert "Neue Kategorie" in order
    assert order[-1] == "Neue Kategorie"
