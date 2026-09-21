"""Temporary history import UI and worker; remove with its app.py call sites.

The worker survives Streamlit reruns, but not a server restart. It only downloads
and transforms data. A UI run merges the finished result into the latest cache,
so a long-running fetch never saves a stale copy of the existing cache.
"""

import shutil
import threading
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from desk_sync import RefreshResult
from service_desks import DESKS, effective_start

START = datetime(2025, 1, 1, tzinfo=ZoneInfo("Europe/Berlin"))


def pull_history(*, fetch=None, transform=None, end=None, progress=None):
    """Fetch all registered desks using the normal pagination and transformation."""
    if fetch is None:
        from jira_loader import fetch_jira_issues

        fetch = fetch_jira_issues
    if transform is None:
        from data_transformation import load_project_issues

        transform = load_project_issues
    end = end or datetime.now(UTC)
    result = RefreshResult(pd.DataFrame())
    frames = []
    for desk in DESKS:

        def report(count, label=desk.label):
            if progress:
                progress(f"{label}: {count} Tickets geladen")

        try:
            report(0)
            issues = fetch(
                effective_start(desk.key, START),
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
        self.status = {"state": "idle"}

    def snapshot(self):
        with self.lock:
            return dict(self.status)

    def start(self):
        with self.lock:
            if self.status["state"] in {"running", "ready"}:
                return False
            self.result = None
            self.status = {"state": "running", "progress": "Import wird gestartet"}
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()
            return True

    def _progress(self, message):
        with self.lock:
            self.status["progress"] = message

    def _run(self):
        try:
            result = pull_history(progress=self._progress)
            with self.lock:
                self.result = result
                self.status = {
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
        from data_transformation import upsert_jira_data

        with self.lock:
            if self.status["state"] != "ready":
                return False
            if not self.result.frame.empty:
                path = Path(data_loading.DATA_PATH)

                def signature():
                    if not path.exists():
                        return None
                    stat = path.stat()
                    return stat.st_ino, stat.st_size, stat.st_mtime_ns

                before = signature()
                current = data_loading.load_data()
                if current is None:
                    current = pd.DataFrame()
                merged = upsert_jira_data(current, self.result.frame)
                if "clone_in_project" not in merged:
                    merged["clone_in_project"] = "-"
                # Keep a concurrently changed cache intact and retry on next poll.
                after = signature()
                if before != after:
                    raise RuntimeError(
                        "Cache wurde parallel geändert; erneut versuchen."
                    )
                if before is not None:
                    backup = f"{path}.bak-history-{datetime.now(UTC):%Y%m%d-%H%M%S-%f}"
                    shutil.copy2(path, backup)
                data_loading.save_data(merged)
                self.status["rows"] = len(merged)
            self.result = None
            self.status["state"] = "completed"
            return True


JOB = HistoryJob()


@st.fragment(run_every=5)
def render_history_pull():
    """The entire removable UI, including its methodology and progress polling."""
    with st.container():
        st.subheader("Temporärer Datenimport ab 2025")
        st.caption(
            "Lädt alle Tickets ab 01.01.2025, 00:00 Uhr (Berlin), bis zum "
            "Start dieses Imports für Ipro, Amparex und Euronet. "
            "Die Auswahl von Firma und Zeitraum begrenzt "
            "diesen Import nicht."
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
                if JOB.apply():
                    st.rerun()
            except Exception as exc:  # noqa: BLE001 - keep result available to retry
                st.error(f"Import konnte nicht gespeichert werden: {exc}")
        state = JOB.snapshot()
        if st.button(
            "📥 Alle Tickets seit 01.01.2025 laden",
            disabled=state["state"] in {"running", "ready"},
            key="temporary_history_start",
        ):
            JOB.start()
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
