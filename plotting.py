import colorsys
from collections.abc import Mapping, Sequence
from typing import Literal

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

import reporting_policy as reporting
from chart_data import ChartData, prepare_chart_data

MODE_ABS = "Absolute Zahlen"
MODE_LOG = "Absolut (log)"
MODE_REL = "Relativ (%)"


def generate_distinct_colors(n: int) -> list[str]:
    """Return evenly spaced categorical colors."""
    # evenly spaced hues in HSV space
    hues = np.linspace(0, 1, n, endpoint=False)
    colors = []
    for h in hues:
        r, g, b = colorsys.hsv_to_rgb(h, 0.65, 0.9)
        colors.append(f"rgb({int(r*255)},{int(g*255)},{int(b*255)})")
    return colors


def create_resolution_time_charts(df: pd.DataFrame) -> list[go.Figure]:
    """Build duration charts accounting for every canonical completed ticket."""
    if "completion_state" not in df or df["completion_state"].isna().any():
        from data_transformation import enrich_jira_time_metrics

        df = enrich_jira_time_metrics(df)
    states = df["completion_state"].astype("string").fillna("unknown")
    counts = {
        "completed": int((states == "done").sum()),
        "open_excluded": int((states == "open").sum()),
        "unknown_completion_excluded": int((~states.isin(["done", "open"])).sum()),
    }
    completed = df.loc[states == "done"].copy()
    bin_col = "time_to_resolution_bin"
    bins = completed.get(bin_col, pd.Series(index=completed.index, dtype="string"))
    bins = bins.astype("string").fillna("Unbekannte Dauer")
    bins = bins.where(bins.isin(reporting.ELAPSED_ORDER), "Unbekannte Dauer")
    bin_order = list(reporting.ELAPSED_ORDER)
    completed[bin_col] = pd.Categorical(bins, categories=bin_order, ordered=True)

    totals = completed.groupby(bin_col, observed=False)["key"].count()
    y_max = max(1, totals.max() if not totals.empty else 0) * 1.15
    figures = []
    for group_col, group_label in [
        (None, None),
        ("request_type", "Quelle"),
        ("source", "Ursprung Ticket"),
    ]:
        group_cols = [bin_col]
        if group_col:
            # Include unset fields so all three charts retain the same totals.
            values = completed.get(
                group_col, pd.Series(index=completed.index, dtype="object")
            )
            completed[group_col] = (
                values.astype("string").fillna("").str.strip().replace("", "Unbekannt")
            )
            group_cols.append(group_col)

        result = (
            completed.groupby(group_cols, observed=False)["key"].count().reset_index()
        )
        title = "Anzahl Fertige Tickets nach Bearbeitungszeit"
        if group_label:
            title += f" – {group_label}"
        fig = px.bar(
            result,
            x=bin_col,
            y="key",
            color=group_col,
            category_orders={bin_col: bin_order},
            labels={
                bin_col: "Bearbeitungszeit in Stunden",
                "key": "Anzahl Fertige Tickets",
                **({group_col: group_label} if group_col else {}),
            },
            title=title,
        )
        fig = apply_font(fig)
        fig.update_layout(barmode="stack", margin=dict(l=100, r=40, b=80))
        fig.update_xaxes(
            type="category",
            categoryorder="array",
            categoryarray=bin_order,
            range=[-0.5, max(len(bin_order) - 0.5, 0.5)],
        )
        fig.update_yaxes(range=[0, y_max])
        fig.update_layout(meta=counts)
        fig.add_annotation(
            text=(
                f"Abgeschlossen: {counts['completed']}; "
                f"offen ausgeschlossen: {counts['open_excluded']}; "
                f"Abschluss unbekannt ausgeschlossen: {counts['unknown_completion_excluded']}"
            ),
            x=0,
            y=1.15,
            xref="paper",
            yref="paper",
            showarrow=False,
        )
        figures.append(fig)
    return figures


def create_toggle_chart(
    df: pd.DataFrame,
    x_col: str,
    group_col: str,
    count_col: str = "key",
    x_label: str | None = None,
    toggle_key: str = "default_key",
    color_map: Mapping[str, str] | None = None,
    force_bottom_value: str | None = None,
    group_order: Sequence[str] | None = None,
    sort_x_by_total: bool = False,
    plot_height: int = 400,
    plot_width: int | None = None,
    allow_log: bool = False,
) -> None:
    """Render the supported distribution-chart UI compatibility adapter."""
    data = prepare_chart_data(
        df,
        x=x_col,
        group=group_col,
        key_col=count_col,
        group_order=group_order or (),
        sort_x_by_total=sort_x_by_total,
    )
    # Compatibility callers historically omit absent groups from their legend.
    present = set(data.table.loc[data.table["count"] > 0, "group"])
    order = [value for value in data.group_order if value in present]
    if force_bottom_value in order:
        order.remove(force_bottom_value)
        order.insert(0, force_bottom_value)
    data = ChartData(data.table, data.x_order, tuple(order), data.totals, data.cohort)
    modes = [MODE_ABS, MODE_LOG, MODE_REL] if allow_log else [MODE_ABS, MODE_REL]
    choice = st.radio(
        "Ansicht wählen:", modes, horizontal=True, index=0, key=toggle_key
    )
    mode: Literal["absolute", "relative", "log"] = (
        "absolute"
        if choice == MODE_ABS
        else "relative" if choice == MODE_REL else "log"
    )
    fig = build_distribution_figure(
        data, mode=mode, colors=color_map, height=plot_height
    )
    fig.update_xaxes(title_text=x_label or x_col)
    st.plotly_chart(
        fig,
        height=plot_height,
        width=plot_width if plot_width is not None else "stretch",
    )


def build_distribution_figure(
    data: ChartData,
    *,
    mode: Literal["absolute", "relative", "log"] = "absolute",
    kind: Literal["bar", "line"] = "bar",
    title: str = "",
    colors: Mapping[str, str] | None = None,
    height: int = 400,
) -> go.Figure:
    """Build a distribution figure without constructing any UI controls.

    Args:
        data: Pure counted memberships and explicit per-stack denominators.
        mode: Absolute counts, relative fractions, or logarithmic count axis.
        kind: Stacked bars or lines sharing the same counted data.
        title: Optional figure title.
        colors: Optional segment-to-color mapping.
        height: Figure height in pixels.

    Returns:
        A fresh figure whose totals and metadata retain the declared cohort.
    """
    if mode not in ("absolute", "relative", "log") or kind not in ("bar", "line"):
        raise ValueError("invalid_distribution_mode")
    fig = go.Figure()
    for group in data.group_order:
        rows = (
            data.table.loc[data.table["group"] == group]
            .set_index("x")
            .reindex(data.x_order)
        )
        counts = rows["count"].fillna(0).tolist()
        percentages = rows["percent"].fillna(0).tolist()
        y = [value / 100 for value in percentages] if mode == "relative" else counts
        labels = [
            f"{count}<br>({percent / 100:.0%})"
            for count, percent in zip(counts, percentages, strict=True)
        ]
        color = colors.get(group) if colors else None
        if kind == "bar":
            fig.add_trace(
                go.Bar(x=data.x_order, y=y, name=group, text=labels, marker_color=color)
            )
        else:
            fig.add_trace(
                go.Scatter(
                    x=data.x_order,
                    y=y,
                    name=group,
                    mode="lines+markers",
                    text=labels,
                    line_color=color,
                )
            )
    fig = apply_font(fig)
    totals = [data.totals[value] for value in data.x_order]
    if data.x_order:
        fig.add_trace(
            go.Scatter(
                x=data.x_order,
                y=(
                    [1 if total else 0 for total in totals]
                    if mode == "relative"
                    else totals
                ),
                text=[f"Total: {total}" for total in totals],
                mode="text",
                name="_totals",
                textposition="top center",
                showlegend=False,
                hoverinfo="skip",
            )
        )
    fig.update_layout(
        title=title,
        height=height,
        barmode="stack",
        legend_title_text="",
        meta={"cohort": data.cohort, "totals": dict(data.totals)},
    )
    fig.update_xaxes(type="category", categoryorder="array", categoryarray=data.x_order)
    if mode == "log":
        fig.update_yaxes(type="log", title_text="Anzahl Tickets")
        fig.add_annotation(
            text="Log-Skala – Balkenanteile sind nicht proportional lesbar",
            xref="paper",
            yref="paper",
            x=0,
            y=1.06,
            showarrow=False,
        )
    else:
        fig.update_yaxes(
            title_text=(
                "Prozentualer Anteil" if mode == "relative" else "Anzahl Tickets"
            ),
            tickformat=".0%" if mode == "relative" else None,
            range=[0, 1.15 if mode == "relative" else max([1, *totals]) * 1.15],
        )
    return fig


def apply_font(fig: go.Figure, base: int = 20) -> go.Figure:
    """Apply shared figure typography and bar-only text properties safely."""
    fig.update_layout(font=dict(size=base))
    fig.update_xaxes(title_font=dict(size=base), tickfont=dict(size=base - 2))
    fig.update_yaxes(title_font=dict(size=base), tickfont=dict(size=base - 2))

    fig.update_traces(
        selector={"type": "bar"},
        textfont_size=base - 2,
        textposition="inside",
        insidetextanchor="middle",
        textangle=0,
    )
    fig.update_layout(
        uniformtext_minsize=base - 2,  # minimum label size
        uniformtext_mode="show",  # do NOT hide or shrink
    )
    fig.update_layout(
        legend=dict(title=dict(font=dict(size=base)), font=dict(size=base - 2))
    )
    fig.update_layout(
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1)
    )
    return fig


def apply_filters(df: pd.DataFrame) -> pd.DataFrame:
    """Filter status and assignee through the legacy sidebar adapter."""
    with st.sidebar:
        status: list[str] = st.multiselect("Status", df.status.unique())
        assignee: list[str] = st.multiselect("Assignee", df.assignee.unique())

    if status:
        df = df[df.status.isin(status)]
    if assignee:
        df = df[df.assignee.isin(assignee)]

    return df
