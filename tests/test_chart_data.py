"""Deterministic chart cohorts, percentages and figures have no UI side effects."""

from typing import Any, Literal
from unittest.mock import patch

import pandas as pd
import plotly.graph_objects as go
import pytest

import plotting


def _frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "key": ["1", "2", "3"],
            "day": pd.Categorical(
                ["a", "a", "b"], categories=["b", "a", "empty"], ordered=True
            ),
            "category": ["short", None, "new"],
        }
    )


def test_counts_percentages_and_missing_groups_reconcile() -> None:
    """Null labels stay visible and each nonempty stack accounts for its cohort."""
    from chart_data import prepare_chart_data

    data = prepare_chart_data(
        _frame(), x="day", group="category", group_order=("short", "absent")
    )
    assert data.cohort == data.table["count"].sum() == 3
    assert dict(data.totals) == {"b": 1, "a": 2, "empty": 0}
    assert data.table.groupby("x")["percent"].sum().to_dict() == {
        "a": 100,
        "b": 100,
        "empty": 0,
    }
    assert "Unbekannt" in data.group_order
    assert data.table.loc[data.table["group"] == "absent", "count"].eq(0).all()
    assert data.table.loc[data.table["x"] == "a", "total"].eq(2).all()


def test_order_and_unknown_groups_are_stable() -> None:
    """Explicit bands survive zero counts and ties have stable label ordering."""
    from chart_data import prepare_chart_data

    frame = pd.DataFrame(
        {"key": ["1", "2"], "country": ["z", "a"], "band": ["new", None]}
    )
    order = ("> 504", "Unbekannte Dauer", "short")
    data = prepare_chart_data(
        frame, x="country", group="band", group_order=order, sort_x_by_total=True
    )
    assert data.x_order == ("a", "z")
    assert data.group_order[:3] == order
    assert set(data.group_order[3:]) == {"new", "Unbekannt"}
    assert data.table["count"].sum() == 2
    reverse = prepare_chart_data(
        frame.iloc[::-1],
        x="country",
        group="band",
        group_order=order,
        sort_x_by_total=True,
    )
    pd.testing.assert_frame_equal(data.table, reverse.table)


@pytest.mark.parametrize("kind", ["bar", "line"])
def test_absolute_relative_log_figures_share_counts(
    kind: Literal["bar", "line"],
) -> None:
    """All modes retain the same counts without applying bar-only style to lines."""
    from chart_data import prepare_chart_data
    from plotting import build_distribution_figure

    data = prepare_chart_data(
        _frame(), x="day", group="category", group_order=("absent",)
    )
    absolute = build_distribution_figure(data, kind=kind)
    relative = build_distribution_figure(data, mode="relative", kind=kind)
    log = build_distribution_figure(data, mode="log", kind=kind)

    def traces(fig: go.Figure) -> list[Any]:
        return [t for t in fig.data if t.name in data.group_order]

    assert sum(sum(t.y) for t in traces(absolute)) == 3
    assert sum(sum(t.y) for t in traces(relative)) == pytest.approx(2)
    assert [tuple(t.y) for t in traces(absolute)] == [tuple(t.y) for t in traces(log)]
    assert log.layout.yaxis.type == "log" and log.layout.yaxis.range is None
    assert all(
        t.type == ("bar" if kind == "bar" else "scatter") for t in traces(absolute)
    )


def test_chart_helpers_are_pure() -> None:
    """Neither preparation nor figure creation touches Streamlit or mutates input."""
    from chart_data import prepare_chart_data
    from plotting import build_distribution_figure

    frame = _frame()
    before = frame.copy(deep=True)
    with (
        patch.object(plotting.st, "radio", side_effect=AssertionError("widget")),
        patch.object(plotting.st, "plotly_chart", side_effect=AssertionError("render")),
    ):
        data = prepare_chart_data(frame, x="day", group="category")
        assert build_distribution_figure(data).layout.height == 400
        empty = prepare_chart_data(pd.DataFrame(), x="day", group="category")
        assert empty.cohort == 0 and not empty.totals
        assert build_distribution_figure(empty).layout.height == 400
    pd.testing.assert_frame_equal(frame, before)
