"""Temporary history import UI and worker; remove with its app.py call sites.

The worker survives Streamlit reruns, but not a server restart. It only downloads
and transforms data. A UI run merges the finished result into the latest cache,
so a long-running fetch never saves a stale copy of the existing cache.
"""

import threading
from datetime import UTC, date, datetime, time
from uuid import uuid4
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from desk_sync import RefreshResult
from service_desks import BY_KEY, DESKS, as_utc, effective_start

START = datetime(2025, 1, 1, tzinfo=ZoneInfo("Europe/Berlin"))


def merge_history(current, incoming, baseline):
    """Keep rows changed during the pull and rows newer than its Jira snapshot."""
    from data_transformation import upsert_jira_data

    current = current if current is not None else pd.DataFrame()
    baseline = baseline if baseline is not None else pd.DataFrame()
    if current.empty or "key" not in current:
        return upsert_jira_data(current, incoming)
    cached = current.drop_duplicates("key", keep="last").set_index("key")
    fresh = incoming.drop_duplicates("key", keep="last").set_index("key")
    overlap = cached.index.intersection(fresh.index)
    protected = pd.Index([])
    if "updated" in cached and "updated" in fresh:
        old_times = pd.to_datetime(
            cached.loc[overlap, "updated"], errors="coerce", utc=True
        )
        new_times = pd.to_datetime(
            fresh.loc[overlap, "updated"], errors="coerce", utc=True
        )
        protected = overlap[
            old_times.notna() & (new_times.isna() | (old_times > new_times))
        ]
    if not baseline.empty and "key" in baseline:
        before = baseline.drop_duplicates("key", keep="last").set_index("key")
        shared = overlap.intersection(before.index)
        columns = cached.columns.union(before.columns, sort=False)
        left = cached.reindex(index=shared, columns=columns).astype(object)
        right = before.reindex(index=shared, columns=columns).astype(object)
        unchanged = (left.eq(right) | (left.isna() & right.isna())).all(axis=1)
        protected = protected.union(shared[~unchanged])
        protected = protected.union(overlap.difference(before.index))
    else:
        # Rows added after an initially empty cache belong to a newer writer.
        protected = protected.union(overlap)
    accepted = incoming.loc[~incoming["key"].isin(protected)]
    return upsert_jira_data(current, accepted)


def import_selection(start, end, projects):
    """Freeze and validate a job's dates and desks before starting any work."""
    start, end = as_utc(start), as_utc(end or datetime.now(UTC))
    projects = tuple(BY_KEY if projects is None else projects)
    if not projects or any(project not in BY_KEY for project in projects):
        raise ValueError("Bitte mindestens einen gültigen Service Desk wählen.")
    if start > end:
        raise ValueError("Das Startdatum muss vor oder auf dem Enddatum liegen.")
    return start, end, tuple(dict.fromkeys(projects))


def pull_history(
    *, start=START, end=None, projects=None, fetch=None, transform=None, progress=None
):
    """Fetch selected desks and dates using normal pagination and transformation."""
    start, end, projects = import_selection(start, end, projects)
    if fetch is None:
        from jira_loader import fetch_jira_issues

        fetch = fetch_jira_issues
    if transform is None:
        from data_transformation import load_project_issues

        transform = load_project_issues
    result = RefreshResult(pd.DataFrame())
    frames = []
    for project in projects:
        desk = BY_KEY[project]

        def report(count, label=desk.label):
            if progress:
                progress(f"{label}: {count} Tickets geladen")

        try:
            report(0)
            issues = fetch(
                effective_start(desk.key, start),
                end,
                project=desk.key,
                max_issues=1_000_000,
                save_path=None,
                progress_cb=report,
            )
            frame = transform(desk.key, issues) if issues else pd.DataFrame()
            if not frame.empty:
                frames.append(frame)
            result.counts[desk.key] = len(issues)
            result.asset_failures[desk.key] = sum(
                bool(issue.get("asset_errors")) for issue in issues
            )
            del issues
        except Exception as exc:  # noqa: BLE001 - desks fail independently
            result.errors[desk.key] = str(exc)
    if frames:
        result.frame = pd.concat(frames, ignore_index=True)
    return result


class HistoryJob:
    """One shared import per server process, independent of browser sessions."""

    def __init__(self):
        self.lock = threading.Lock()
        self.thread = None
        self.result = None
        self.baseline = None
        self.completion = None
        self.status = {"state": "idle"}

    def snapshot(self):
        with self.lock:
            return {**self.status, "completion": self.completion}

    def start(self, *, start=START, end=None, projects=None):
        from data_loading import load_data_snapshot

        with self.lock:
            if self.status["state"] in {"running", "ready"}:
                return False
            start, end, projects = import_selection(start, end, projects)
            self.result = None
            self.baseline, _ = load_data_snapshot()
            self.status = {
                "state": "running",
                "progress": "Import wird gestartet",
                "start": start,
                "end": end,
                "projects": projects,
            }
            self.thread = threading.Thread(
                target=self._run, args=(start, end, projects), daemon=True
            )
            self.thread.start()
            return True

    def _progress(self, message):
        with self.lock:
            self.status["progress"] = message

    def _run(self, start, end, projects):
        try:
            result = pull_history(
                start=start, end=end, projects=projects, progress=self._progress
            )
            with self.lock:
                self.result = result
                self.status = {
                    **self.status,
                    "state": "ready",
                    "counts": dict(result.counts),
                    "errors": dict(result.errors),
                    "asset_failures": dict(result.asset_failures),
                }
        except Exception as exc:  # noqa: BLE001 - report worker failure to the UI
            with self.lock:
                self.status = {"state": "failed", "errors": {"Import": str(exc)}}

    def apply(self):
        """Apply once, backing up the latest cache and preserving unrelated rows."""
        import data_loading

        with self.lock:
            if self.status["state"] != "ready":
                return False
            if not self.result.frame.empty:
                with data_loading.cache_lock():
                    current, revision = data_loading.load_data_snapshot()
                    merged = merge_history(current, self.result.frame, self.baseline)
                    if "clone_in_project" not in merged:
                        merged["clone_in_project"] = "-"
                    data_loading.save_data_if_unchanged(
                        merged, revision, backup_prefix="history"
                    )
                    self.status["rows"] = len(merged)
            self.result = None
            self.baseline = None
            self.completion = uuid4().hex
            self.status["state"] = "completed"
            return True


JOB = HistoryJob()


@st.fragment(run_every=5)
def render_history_pull():
    """The entire removable UI, including its methodology and progress polling."""
    with st.container():
        st.subheader("Backfill / Datenimport")
        st.caption(
            "Lädt Tickets, die im gewählten Zeitraum erstellt wurden, für die "
            "gewählten Service Desks. Beide Tage zählen vollständig (Berlin); "
            "für heute werden Tickets bis zum Importstart geladen. "
            "Diese Auswahl ist unabhängig von den Filtern in der Seitenleiste."
        )
        st.caption(
            "Vorhandene Tickets werden über ihren Jira-Schlüssel aktualisiert, "
            "neue ergänzt. Ältere Tickets bleiben erhalten. Vor dem Speichern "
            "wird eine Sicherung erstellt. Für historische Statistiken danach "
            "den Zeitraum in der Seitenleiste erweitern."
        )
        state = JOB.snapshot()
        if state["state"] == "ready":
            try:
                JOB.apply()
            except Exception as exc:  # noqa: BLE001 - keep result available to retry
                st.error(f"Import konnte nicht gespeichert werden: {exc}")
        state = JOB.snapshot()
        completion = state["completion"]
        if completion and st.session_state.get("_history_completion") != completion:
            st.session_state["_history_completion"] = completion
            st.rerun()
        busy = state["state"] in {"running", "ready"}
        now = datetime.now(START.tzinfo)
        picked = st.date_input(
            "Backfill-Zeitraum (erstellt)",
            value=(START.date(), now.date()),
            min_value=date(1970, 1, 1),
            max_value=now.date(),
            format="DD.MM.YYYY",
            disabled=busy,
            key="temporary_history_dates",
        )
        projects = st.multiselect(
            "Service Desks",
            options=list(BY_KEY),
            default=list(BY_KEY),
            format_func=lambda project: f"{BY_KEY[project].label} ({project})",
            disabled=busy,
            key="temporary_history_desks",
        )
        if len(picked) != 2:
            st.info("Bitte Start- und Enddatum für den Import wählen.")
        if not projects:
            st.info("Bitte mindestens einen Service Desk für den Import wählen.")
        if "projects" in state:
            labels = ", ".join(BY_KEY[p].label for p in state["projects"])
            st.caption(
                f"Letzter/laufender Import: {labels}, "
                f"{state['start'].astimezone(START.tzinfo):%d.%m.%Y} – "
                f"{state['end'].astimezone(START.tzinfo):%d.%m.%Y}."
            )
        if st.button(
            "📥 Ausgewählte Tickets laden",
            disabled=busy or len(picked) != 2 or not projects,
            key="temporary_history_start",
        ):
            start = datetime.combine(picked[0], time.min, tzinfo=START.tzinfo)
            end = min(datetime.combine(picked[1], time.max, tzinfo=START.tzinfo), now)
            JOB.start(start=start, end=end, projects=projects)
            st.rerun()
        if state["state"] == "running":
            st.info(f"Import läuft im Hintergrund. {state.get('progress', '')}")
            st.caption(
                "Der Import kann lange dauern. Seitenwechsel unterbrechen ihn "
                "nicht; ein Server-Neustart bricht ihn ab. Zum Übernehmen der "
                "Ergebnisse diese Seite geöffnet lassen oder später zurückkehren."
            )
            st.button("Importstatus aktualisieren", key="temporary_history_status")
        if state["state"] == "completed":
            message = f"{sum(state['counts'].values())} Tickets geladen. " + (
                f"{state['rows']} Tickets im Cache."
                if "rows" in state
                else "Keine Änderungen am Cache."
            )
            if state["errors"] or any(state["asset_failures"].values()):
                st.warning(message + " Import unvollständig; Details unten beachten.")
            else:
                st.success(message)
            for desk in DESKS:
                if desk.key in state["counts"]:
                    st.write(f"{desk.label}: {state['counts'][desk.key]} Tickets")
                failures = state["asset_failures"].get(desk.key, 0)
                if failures:
                    st.warning(
                        f"{desk.label}: Assets-Daten bei {failures} Tickets "
                        "unvollständig. Kategorien können Unbekannt bleiben."
                    )
        for project, error in state.get("errors", {}).items():
            st.error(f"{project}: {error}")
