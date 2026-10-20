"""Pure distribution data with explicit per-stack reporting denominators."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

import pandas as pd


@dataclass(frozen=True)
class ChartData:
    """Counted memberships, stable display orders and per-x denominators."""

    table: pd.DataFrame
    x_order: tuple[str, ...]
    group_order: tuple[str, ...]
    totals: Mapping[str, int]
    cohort: int


def prepare_chart_data(
    frame: pd.DataFrame,
    *,
    x: str,
    group: str,
    key_col: str = "key",
    group_order: Sequence[str] = (),
    sort_x_by_total: bool = False,
    unknown_label: str = "Unbekannt",
) -> ChartData:
    """Count supplied memberships without dropping missing display categories.

    Args:
        frame: Selected tickets, or explicitly exploded category memberships.
        x: Stack/period column.
        group: Segment column.
        key_col: Nonmissing keys contribute one count per supplied membership.
        group_order: Explicit groups, retained even when their count is zero.
        sort_x_by_total: Sort decreasing totals with stable label ties.
        unknown_label: Display label for missing or blank categories.

    Returns:
        A fresh table with x/group/count/percent/total columns. Percent is 0–100
        against each x total; empty stacks have zero percent.
    """

    def labels(values: pd.Series) -> pd.Series:
        return values.astype("string").fillna("").str.strip().replace("", unknown_label)

    def order(column: str, values: pd.Series) -> list[str]:
        if column in frame and isinstance(frame[column].dtype, pd.CategoricalDtype):
            categories = labels(pd.Series(frame[column].cat.categories)).tolist()
            return list(dict.fromkeys([*categories, *sorted(values.unique())]))
        return sorted(values.unique())

    work = pd.DataFrame(
        {
            "x": labels(frame[x]) if x in frame else pd.Series(dtype="string"),
            "group": (labels(frame[group]) if group in frame else pd.Series(dtype="string")),
            "key": frame[key_col] if key_col in frame else pd.Series(dtype="string"),
        }
    )
    if not frame.empty and not {x, group, key_col}.issubset(frame.columns):
        raise ValueError("missing_chart_columns")
    x_order = order(x, work["x"])
    groups = list(dict.fromkeys([*group_order, *order(group, work["group"])]))
    counts = work.groupby(["x", "group"], observed=True)["key"].count()
    counted_totals = counts.groupby(level="x").sum()
    totals = {value: int(counted_totals.get(value, 0)) for value in x_order}
    if sort_x_by_total:
        x_order.sort(key=lambda value: (-totals[value], value))
    records = []
    for value in x_order:
        total = totals[value]
        for segment in groups:
            count = int(counts.get((value, segment), 0))
            records.append(
                {
                    "x": value,
                    "group": segment,
                    "count": count,
                    "percent": count * 100 / total if total else 0.0,
                    "total": total,
                }
            )
    table = pd.DataFrame(records, columns=["x", "group", "count", "percent", "total"])
    return ChartData(
        table,
        tuple(x_order),
        tuple(groups),
        MappingProxyType(totals),
        int(work["key"].count()),
    )
