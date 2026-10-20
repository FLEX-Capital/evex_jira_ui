"""Focused dashboard views over explicit filtered/full-cache context."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from st_aggrid import AgGrid, GridOptionsBuilder

import asset_country
import interactive
from chart_data import ChartData, prepare_chart_data
from country_cache import CountryCacheError
from file_io import FileSnapshotChangedError, Revision
from maintenance_ui import render_import_empty_state, render_maintenance
from plotting import (
    MODE_ABS,
    MODE_LOG,
    MODE_REL,
    build_distribution_figure,
    create_resolution_time_charts,
    generate_distinct_colors,
)
from resolution_bands import BAND_COLORS, BAND_ORDER
from settings import Settings
from ui_auth import require_current_session


@dataclass(frozen=True)
class DashboardContext:
    """Validated page data, settings and display choices passed to every view."""

    frame: pd.DataFrame
    full_frame: pd.DataFrame
    revision: Revision | None
    settings: Settings
    x_axis: str
    x_label: str
    plot_height: int = 900
    plot_width: int = 1500


def _mode(key: str, *, allow_log: bool = False) -> Literal["absolute", "relative", "log"]:
    choice = st.radio(
        "Ansicht wählen:",
        [MODE_ABS, MODE_LOG, MODE_REL] if allow_log else [MODE_ABS, MODE_REL],
        horizontal=True,
        index=0,
        key=key,
    )
    return "relative" if choice == MODE_REL else "log" if choice == MODE_LOG else "absolute"


def _show(context: DashboardContext, figure: go.Figure, x_label: str | None = None) -> None:
    figure.update_layout(height=context.plot_height)
    figure.update_xaxes(title_text=x_label or context.x_label)
    st.plotly_chart(figure, height=context.plot_height, width=context.plot_width)


def render_overview(context: DashboardContext) -> None:
    """Render status distribution over all selected tickets."""
    st.header("📊 Überblick")
    data = prepare_chart_data(
        context.frame, x=context.x_axis, group="status", group_order=("Fertig",)
    )
    _show(context, build_distribution_figure(data, mode=_mode("toggle_overview")))


def render_categories(context: DashboardContext) -> None:
    """Render resolution groups per main category, counting selected tickets."""
    st.header("📊 Aufteilung Kategorien")
    data = prepare_chart_data(
        context.frame,
        x="Hauptkategorie",
        group="resolution",
        group_order=("Same day",),
        sort_x_by_total=True,
    )
    figure = build_distribution_figure(
        data,
        mode=_mode("toggle_categories"),
        colors={"Same day": "green", "> 1 day": "#FFD700"},
    )
    _show(context, figure, "Hauptkategorie")


def render_subcategories(context: DashboardContext) -> None:
    """Render visible subcategories without dropping uncategorized selected tickets."""
    st.header("📊 Aufteilung Unterkategorien")
    data = prepare_chart_data(
        context.frame, x="Hauptkategorie", group="Unterkategorie", sort_x_by_total=True
    )
    colors = dict(
        zip(
            data.group_order,
            generate_distinct_colors(len(data.group_order)),
            strict=True,
        )
    )
    _show(context, build_distribution_figure(data, colors=colors), "Hauptkategorie")


def render_sources(context: DashboardContext) -> None:
    """Render the existing known-request-type ticket cohort."""
    st.header("📊 Aufteilung Quellen (Anfragetyp)")
    known = context.frame["request_type"].astype("string").fillna("").str.strip().ne("")
    frame = context.frame.loc[known]
    st.caption(
        f"Basis: {len(frame)} Tickets mit Anfragetyp; "
        f"{int((~known).sum())} ohne Anfragetyp ausgeschlossen."
    )
    data = prepare_chart_data(frame, x=context.x_axis, group="request_type")
    _show(context, build_distribution_figure(data, mode=_mode("toggle_sources")))


def render_origin(context: DashboardContext) -> None:
    """Render all selected tickets, including an unknown origin segment."""
    st.header("📊 Aufteilung Ursprung Ticket")
    data = prepare_chart_data(context.frame, x=context.x_axis, group="source")
    _show(context, build_distribution_figure(data, mode=_mode("toggle_ursprung")))


def render_countries(context: DashboardContext) -> None:
    """Render country/duration groups with the independent include-open choice."""
    st.header("🌍 Aufteilung Länder")
    try:
        status = asset_country.country_cache_status(
            context.full_frame,
            asset_country.load_cache(
                path=context.settings.paths.countries, settings=context.settings
            ),
            settings=context.settings,
        )
        st.caption(
            f"Länder-Cache: {status['stale']} veraltet, "
            f"{status['unverified']} unbestätigt, {status['unresolved']} ungeklärt."
        )
    except (CountryCacheError, FileSnapshotChangedError, OSError):
        st.warning(
            "Länder-Cache konnte nicht gelesen werden. "
            "Bitte Format prüfen oder Sicherung wiederherstellen."
        )
    missing = [column for column in ("Land", "resolution_band") if column not in context.frame]
    if missing:
        st.info(
            f"Länderdaten fehlen (Spalten: {', '.join(missing)}). "
            "Bitte `backfill_country.py` ausführen oder Daten aktualisieren."
        )
        return
    include_open = st.checkbox(
        "Nicht abgeschlossene Tickets einblenden", value=True, key="land_include_open"
    )
    frame = (
        context.frame
        if include_open
        else context.frame.loc[context.frame["completion_state"] == "done"]
    )
    land = (
        frame["Land"]
        .astype("string")
        .fillna("")
        .str.strip()
        .replace("", asset_country.NOT_RESOLVED)
    )
    frame = frame.assign(Land=land)
    pending = int(land.eq(asset_country.NOT_RESOLVED).sum())
    if pending:
        st.warning(
            f"{pending} von {len(frame)} Tickets haben noch kein aufgelöstes Land "
            f"und stehen als „{asset_country.NOT_RESOLVED}“ in der Grafik. "
            "Über „Länder aktualisieren“ erneut prüfen."
        )
    if frame.empty:
        st.info("Keine Tickets im gewählten Zeitraum.")
    data = prepare_chart_data(
        frame,
        x="Land",
        group="resolution_band",
        group_order=BAND_ORDER,
        sort_x_by_total=True,
    )
    _show(
        context,
        build_distribution_figure(
            data, mode=_mode("toggle_countries", allow_log=True), colors=BAND_COLORS
        ),
        "Land",
    )


def render_status(context: DashboardContext) -> None:
    """Render known-open selected tickets, excluding completed and unknown completion."""
    st.header("📊 Offene Tickets nach Status")
    frame = context.frame.loc[context.frame["completion_state"] == "open"]
    data = prepare_chart_data(frame, x="status_category", group="status")
    _show(context, build_distribution_figure(data), "Statuskategorie")


def render_cycle_time(context: DashboardContext) -> None:
    """Retain canonical completed-ticket duration cohorts and explicit exclusions."""
    st.header("⏱️ Ticketbearbeitungszeit (Fertige Tickets)")
    for figure in create_resolution_time_charts(context.frame):
        st.plotly_chart(figure, height=context.plot_height, width=context.plot_width)


def render_resolution(context: DashboardContext) -> None:
    """Render the existing all-ticket first-resolution distribution."""
    st.header("📈 Erstlösequote")
    data = prepare_chart_data(
        context.frame,
        x=context.x_axis,
        group="resolution",
        group_order=("Same day",),
        sort_x_by_total=True,
    )
    _show(
        context,
        build_distribution_figure(
            data,
            mode=_mode("toggle_resolution_time"),
            colors={"Same day": "green", "> 1 day": "#FFD700"},
        ),
    )


def render_customers(context: DashboardContext) -> None:
    """Render the twenty-five largest customer cohorts among completed tickets."""
    st.header("📚 Anzahl Tickets pro Kunde")
    frame = context.frame.loc[context.frame["completion_state"] == "done"].assign(
        _chart_group="Abgeschlossen"
    )
    data = prepare_chart_data(frame, x="zentrale", group="_chart_group", sort_x_by_total=True)
    top = data.x_order[:25]
    totals = {key: data.totals[key] for key in top}
    data = ChartData(
        data.table.loc[data.table["x"].isin(top)].copy(),
        top,
        data.group_order,
        totals,
        sum(totals.values()),
    )
    _show(context, build_distribution_figure(data), "zentrale")


def render_clones(context: DashboardContext) -> None:
    """Render normalized clone-project memberships, preserving ticket versus membership counts."""
    st.header("📊 Clone Tickets")
    frame = context.frame
    known = (
        frame["clone_count"].notna()
        if "clone_count" in frame
        else pd.Series(False, index=frame.index)
    )
    st.caption(
        f"{int((~known).sum())} Tickets wegen fehlender typisierter Beziehungsdaten ausgeschlossen."
    )
    members = frame.loc[known].copy()
    projects = []
    for _, row in members.iterrows():
        keys = [*row["clone_inward_keys"], *row["clone_outward_keys"]]
        projects.append(list(dict.fromkeys(key.rsplit("-", 1)[0] for key in keys)) or ["-"])
    members["clone_in_project"] = pd.Series(projects, index=members.index, dtype=object)
    members = members.explode("clone_in_project")
    st.caption(f"Basis: {len(members)} Projektmitgliedschaften aus {int(known.sum())} Tickets.")
    data = prepare_chart_data(
        members.assign(_chart_group="Projektmitgliedschaften"),
        x="clone_in_project",
        group="_chart_group",
        sort_x_by_total=True,
    )
    _show(context, build_distribution_figure(data), "clone_in_project")
    data = prepare_chart_data(members, x=context.x_axis, group="clone_in_project")
    _show(context, build_distribution_figure(data))


def _link_first(frame: pd.DataFrame) -> pd.DataFrame:
    columns = (
        ["Link", *[column for column in frame.columns if column != "Link"]]
        if "Link" in frame
        else list(frame.columns)
    )
    return frame.loc[:, columns]


def render_raw(context: DashboardContext) -> None:
    """Show the selected frame with its Jira link first, without mutating context."""
    st.header("📄 Rohdaten")
    st.dataframe(
        _link_first(context.frame),
        column_config={
            "Link": st.column_config.LinkColumn("JIRA Link", display_text="Open in JIRA")
        },
        hide_index=True,
        height=context.plot_height,
    )


def render_interactive_view(context: DashboardContext) -> None:
    """Keep maintenance independent and filter collections from both interactive consumers."""
    st.header("📄 Interaktiv")
    render_maintenance(context.settings)
    if context.frame.empty:
        st.info("Keine Daten im gewählten Zeitraum.")
        return
    problem = [
        column
        for column in context.frame
        if any(isinstance(value, (list, dict, set)) for value in context.frame[column])
    ]
    frame = _link_first(context.frame.drop(columns=problem))
    interactive.render_interactive(frame)
    st.write("\n\n\n\n\n\n")
    builder = GridOptionsBuilder.from_dataframe(frame)
    builder.configure_default_column(filter=True, sortable=True)
    AgGrid(
        frame,
        gridOptions=builder.build(),
        height=context.plot_height,
        width=context.plot_width,
    )


def render_dashboard(context: DashboardContext) -> None:
    """Render the thirteen established tabs over one explicit page context."""
    require_current_session()
    if context.frame.empty:
        render_import_empty_state("Keine Tickets im gewählten Zeitraum.")
        return
    views: list[tuple[str, Callable[[DashboardContext], None]]] = [
        ("📊 Überblick", render_overview),
        ("📊 Kategorien", render_categories),
        ("📊 Unterkategorien", render_subcategories),
        ("📊 Quellen", render_sources),
        ("📊 Ursprung", render_origin),
        ("🌍 Länder", render_countries),
        ("📊 Offene Tickets nach Status", render_status),
        ("⏱️ Ticketbearbeitungszeit", render_cycle_time),
        ("📈 Erstlösequote", render_resolution),
        ("📚 Tickets pro Kunde", render_customers),
        ("📊 Clone Tickets", render_clones),
        ("📄 Rohdaten", render_raw),
        ("📄 Interaktiv", render_interactive_view),
    ]
    with st.container(key="dashboard"):
        for tab, (_, renderer) in zip(st.tabs([label for label, _ in views]), views, strict=True):
            with tab:
                renderer(context)
