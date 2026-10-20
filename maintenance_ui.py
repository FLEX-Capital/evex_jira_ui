"""Authenticated optional maintenance, separate from filtered dashboard frames."""

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from functools import partial
from pathlib import Path
from types import MappingProxyType
from typing import Literal
from uuid import uuid4

import pandas as pd
import streamlit as st

import asset_country
import data_loading
import source_sync
from asset_country import NOT_RESOLVED
from data_loading import CacheChangedError, load_data_snapshot, save_data_if_unchanged
from operation_errors import OperationDiagnostic, safe_diagnostic
from settings import Settings, load_settings
from source_sync import fetch_issue_sources
from temporary_history_pull import render_history_pull
from ui_auth import require_current_session


@dataclass(frozen=True)
class MaintenanceOutcome:
    """Changed rows, incomplete work and payload-free causes from one operation."""

    kind: Literal["source", "country"]
    changed: int
    incomplete: bool
    diagnostics: tuple[OperationDiagnostic, ...]
    backup: Path | None = None
    counts: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "counts", MappingProxyType(dict(self.counts)))


def refresh_source_cache(path: Path, *, settings: Settings | None = None) -> MaintenanceOutcome:
    """Prepare full-cache source updates outside locks, then conditionally publish.

    Args:
        path: Explicit ticket cache, independent of display filters.
        settings: Optional already captured operation settings.

    Returns:
        Changed row count and any remaining blank-source count.
    """
    require_current_session()
    config = settings or load_settings()
    path = path.resolve()
    frame, revision = load_data_snapshot(path=path, settings=config)
    frame = frame if frame is not None else pd.DataFrame()
    prepared, changed = source_sync.refresh_missing_sources(
        frame, fetch_sources=partial(fetch_issue_sources, settings=config)
    )
    require_current_session()
    backup = (
        save_data_if_unchanged(prepared, revision, path=path, settings=config) if changed else None
    )
    remaining = (
        int(prepared["source"].astype("string").fillna("").str.strip().eq("").sum())
        if "source" in prepared
        else 0
    )
    return MaintenanceOutcome(
        "source",
        changed,
        bool(remaining),
        (),
        Path(backup) if backup is not None else None,
        {"remaining": remaining},
    )


def refresh_country_cache(
    path: Path,
    *,
    force: bool = False,
    settings: Settings | None = None,
) -> MaintenanceOutcome:
    """Retain independent country work even when a newer ticket revision refuses publication.

    Args:
        path: Complete ticket-cache artifact.
        force: Explicitly refresh even fresh verified country entries.
        settings: Optional captured settings for both ticket and country paths.

    Returns:
        Changed ticket rows, safe aggregate causes and existing country counters.
    """
    require_current_session()
    config = settings or load_settings()
    path = path.resolve()
    frame, revision = load_data_snapshot(path=path, settings=config)
    frame = frame if frame is not None else pd.DataFrame()
    before = (
        frame.get("Land", pd.Series(index=frame.index, dtype="string")).astype("string").fillna("")
    )
    baseline = asset_country.load_cache(path=config.paths.countries, settings=config)
    prepared, cache, stats = asset_country.refresh_countries(
        frame,
        cache=baseline,
        cache_path=config.paths.countries,
        force=force,
        settings=config,
    )
    require_current_session()
    cache = asset_country.save_cache(
        cache, path=config.paths.countries, baseline=baseline, settings=config
    )
    if data_loading.cache_revision(path) != revision:
        raise CacheChangedError("cache_changed")
    prepared = asset_country.attach_country(prepared, cache, allow_legacy=True, settings=config)
    changed = int(prepared["Land"].astype("string").fillna("").ne(before).sum())
    require_current_session()
    # Retain the revision check even on unchanged Land: a concurrently replaced
    # snapshot must be reported for retry instead of claiming the older run is current.
    backup = save_data_if_unchanged(prepared, revision, path=path, settings=config)
    stats = {
        **stats,
        "rows": changed,
        "pending": int(prepared["Land"].eq(NOT_RESOLVED).sum()),
    }
    diagnostics = (
        (OperationDiagnostic(uuid4().hex, "assets", "unexpected"),) if stats["failed"] else ()
    )
    return MaintenanceOutcome(
        "country",
        changed,
        bool(stats["failed"] or stats["pending"]),
        diagnostics,
        Path(backup) if backup is not None else None,
        stats,
    )


def _show_diagnostic(cause: OperationDiagnostic) -> None:
    st.session_state["maintenance_diagnostic"] = asdict(cause)
    st.caption(f"{cause.stage} · {cause.code} · Run ID: {cause.operation_id}")


def _maintenance_failure(label: str, stage: str, exc: Exception) -> None:
    cause = safe_diagnostic(exc, operation_id=uuid4().hex, stage=stage)
    _show_diagnostic(cause)
    message = f"{label} fehlgeschlagen ({cause.code})."
    if cause.code == "cache_changed":
        message += " Cache wurde parallel geändert; bitte die Aktualisierung wiederholen."
    st.error(message)


def render_import_empty_state(message: str) -> None:
    """Show an empty-state message while retaining independent history-import controls.

    Args:
        message: Existing user-visible reason no dashboard rows are displayed.
    """
    require_current_session()
    st.info(message)
    with (
        st.tabs(["📄 Interaktiv"])[0],
        st.expander("Daten aktualisieren", expanded=False),
    ):
        render_history_pull()


def render_maintenance(settings: Settings) -> None:
    """Render optional maintenance and its safe feedback using captured paths.

    Args:
        settings: Validated settings captured by the page entry point.
    """
    require_current_session()
    if "source_sync_success" in st.session_state:
        st.success(
            f"{st.session_state.pop('source_sync_success')} fehlende Ursprünge aus Jira übernommen."
        )
    if "source_sync_remaining" in st.session_state:
        st.warning(
            f"{st.session_state.pop('source_sync_remaining')} Tickets bleiben ohne Ursprung."
        )
    if "country_sync_success" in st.session_state:
        stats = st.session_state.pop("country_sync_success")
        st.success(
            f"Länder aktualisiert: {stats['resolved']} Assets neu aufgelöst, "
            f"{stats['rows']} Tickets neu zugeordnet."
        )
        if stats["failed"]:
            st.warning(
                f"{stats['failed']} Asset-Abfragen fehlgeschlagen. Sie sind nicht "
                "zwischengespeichert - erneut ausführen, um sie zu wiederholen."
            )
        if stats["pending"]:
            st.warning(
                f"{stats['pending']} Tickets bleiben „{NOT_RESOLVED}“ "
                "(fehlgeschlagene oder ungeklärte Abfragen)."
            )
    for cause in st.session_state.pop("maintenance_causes", ()):
        _show_diagnostic(cause)

    with st.expander("Daten aktualisieren", expanded=False):
        render_history_pull()
        col_sources, col_countries = st.columns(2)
        with col_sources:
            refresh_sources = st.button(
                "🔄 Fehlenden Ursprung aktualisieren",
                help=(
                    "Prüft alle gespeicherten Tickets mit leerem Ursprung in Jira, "
                    "unabhängig vom gewählten Zeitraum und der Firma."
                ),
            )
        with col_countries:
            force = st.checkbox("Bekannte Länder erneut prüfen", key="country_force_refresh")
            refresh_country = st.button(
                "🌍 Länder aktualisieren",
                help=(
                    "Prüft unbekannte oder veraltete Assets und ordnet Land für ALLE "
                    "gespeicherten Tickets zu, unabhängig von Firma und Zeitraum."
                ),
            )
    if refresh_sources:
        try:
            with st.spinner("Fehlende Ursprünge werden mit Jira abgeglichen …"):
                outcome = refresh_source_cache(settings.paths.tickets, settings=settings)
            st.session_state["source_sync_success"] = outcome.changed
            if outcome.incomplete:
                st.session_state["source_sync_remaining"] = outcome.counts.get("remaining", 0)
        except Exception as exc:  # noqa: BLE001 - isolate optional maintenance
            _maintenance_failure("Ursprung-Abgleich", "source", exc)
        else:
            st.rerun()
    if refresh_country:
        try:
            with st.spinner("Assets werden aufgelöst und Länder neu zugeordnet …"):
                outcome = refresh_country_cache(
                    settings.paths.tickets, force=force, settings=settings
                )
            st.session_state["country_sync_success"] = {
                **outcome.counts,
                "rows": outcome.changed,
            }
            st.session_state["maintenance_causes"] = outcome.diagnostics
        except Exception as exc:  # noqa: BLE001 - isolate optional maintenance
            _maintenance_failure("Länder-Abgleich", "assets", exc)
        else:
            st.rerun()
