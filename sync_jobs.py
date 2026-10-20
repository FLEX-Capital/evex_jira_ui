"""Shared UI execution, durable status observation, and import feedback."""

import json
import os
import threading
from datetime import datetime
from pathlib import Path

import streamlit as st

import data_loading
from desk_sync import make_sync_request, run_sync, status_path
from service_desks import BY_KEY
from ui_auth import require_current_session


def _status_revision(data_path):
    try:
        return data_loading.cache_revision(status_path(data_path))
    except OSError:
        return None


class SyncJobs:
    def __init__(self):
        self.lock = threading.Lock()
        self.thread = None
        self.failure = None

    def start(self, request):
        data_path = Path(data_loading.DATA_PATH).resolve()
        admitted = threading.Event()
        accepted = []
        observed_revision = _status_revision(data_path)

        def progress(event):
            nonlocal observed_revision
            observed_revision = _status_revision(data_path)
            if event["phase"] == "admitted":
                accepted.append(True)
                admitted.set()

        def work():
            try:
                run_sync(request, progress=progress, data_path=data_path)
            except data_loading.ImportBusyError:
                pass
            except (
                Exception
            ) as exc:  # noqa: BLE001 - expose pre-admission/storage failures
                with self.lock:
                    self.failure = (
                        data_path,
                        observed_revision,
                        {
                            "state": "failed",
                            "request": request.to_dict(),
                            "progress": f"Import fehlgeschlagen: {type(exc).__name__}",
                            "report": {
                                "save_error": type(exc).__name__,
                                "complete": False,
                            },
                        },
                    )
            finally:
                admitted.set()

        with self.lock:
            if self.thread is not None and self.thread.is_alive():
                return False
            self.failure = None
            self.thread = threading.Thread(target=work, daemon=True)
            self.thread.start()
        # No network occurs before admission. A competing CLI/UI reports rejection.
        admitted.wait()
        return bool(accepted)

    def snapshot(self):
        path = Path(data_loading.DATA_PATH).resolve()
        revision = None
        try:
            with status_path(path).open() as handle:
                status = json.load(handle)
                stat = os.fstat(handle.fileno())
                revision = (stat.st_ino, stat.st_size, stat.st_mtime_ns)
        except FileNotFoundError:
            status = {"state": "idle"}
        except (OSError, ValueError):
            revision = _status_revision(path)
            status = {
                "state": "failed",
                "progress": "Importstatus konnte nicht gelesen werden.",
            }
        with self.lock:
            failure = self.failure
            if failure is not None and failure[0] == path and failure[1] != revision:
                # Another UI/CLI run has replaced the status we last observed.
                self.failure = None
                failure = None
        if failure is not None and failure[0] == path:
            try:
                with data_loading.import_operation_lock(path):
                    pass
            except data_loading.ImportBusyError:
                return {"state": "running", "progress": "Ein Datenimport läuft."}
            except OSError:
                pass
            return dict(failure[2])
        if status.get("state") == "running":
            try:
                with data_loading.import_operation_lock(path):
                    status["state"] = "interrupted"
            except data_loading.ImportBusyError:
                pass
        return status

    def resume(self):
        status = self.snapshot()
        if status.get("state") not in {"interrupted", "partial", "failed"}:
            return False
        saved = dict(status.get("request") or {})
        if not saved:
            return False
        try:
            saved["start"] = datetime.fromisoformat(saved["start"])
            saved["end"] = datetime.fromisoformat(saved["end"])
            saved["resume"] = True
            return self.start(make_sync_request(**saved))
        except (ValueError, TypeError, KeyError):
            return False


JOB = SyncJobs()


def render_sync_feedback(location):
    status = JOB.snapshot()
    token = (status.get("run_id"), status.get("state"))
    previous = st.session_state.get("_sync_seen_run_state")
    st.session_state["_sync_seen_run_state"] = token
    finished = (
        previous is not None
        and previous[0] == token[0]
        and previous[1] == "running"
        and token[1] != "running"
    )
    revision = data_loading.cache_revision()
    if "_sync_seen_revision" not in st.session_state:
        st.session_state["_sync_seen_revision"] = revision
    elif st.session_state["_sync_seen_revision"] != revision or finished:
        st.session_state["_sync_seen_revision"] = revision
        st.rerun()
    state = status.get("state", "idle")
    if state == "idle":
        return
    saved = status.get("request") or {}
    if saved:
        labels = ", ".join(
            BY_KEY[p].label for p in saved.get("projects", []) if p in BY_KEY
        )
        st.caption(f"Letzter/laufender Import: {labels}")
    if state == "running":
        st.info(f"Import läuft im Hintergrund. {status.get('progress', '')}")
        return
    if state == "interrupted":
        st.warning(
            "Import durch einen Server-Neustart unterbrochen. Gespeicherter Fortschritt kann fortgesetzt werden."
        )
    report = status.get("report") or {}
    if report:
        message = f"{sum(report.get('counts', {}).values())} Tickets geladen. {report.get('rows', 0)} Tickets im Cache."
        if report.get("complete"):
            st.success(message)
        else:
            st.warning(message + " Import unvollständig; Details unten beachten.")
        categories = report.get("categories") or {}
        st.caption(
            f"Kategorien: {categories.get('repaired_cells', 0)} ergänzt, "
            f"{categories.get('unassigned_cells', 0)} nicht zugeordnet, "
            f"{categories.get('unresolved_cells', 0)} noch unbekannt, "
            f"{len(categories.get('failed_objects', {}))} fehlgeschlagene Referenzen/Objekte."
        )
        protected = report.get("protected_rows", 0)
        skipped = categories.get("skipped_cells", 0)
        if protected or skipped:
            st.info(
                f"Parallele Änderungen beibehalten: {protected} Tickets, {skipped} Kategorieänderungen übersprungen."
            )
        for project, count in report.get("counts", {}).items():
            st.write(
                f"{BY_KEY[project].label if project in BY_KEY else project}: {count} Tickets"
            )
        for project, error in report.get("errors", {}).items():
            st.error(
                f"{BY_KEY[project].label if project in BY_KEY else project}: {error}"
            )
        for project, count in report.get("asset_failures", {}).items():
            if count:
                st.warning(
                    f"{project}: Assets-Daten bei {count} Tickets unvollständig. Jira-/Assets-Berechtigungen prüfen."
                )
        for name in ("source_error", "save_error", "raw_error"):
            if report.get(name):
                st.error(f"{name}: {report[name]}")
    elif state == "failed":
        st.error(status.get("progress", "Import fehlgeschlagen."))
    if state in {"interrupted", "partial", "failed"} and saved:
        if st.button("⏯ Import fortsetzen", key=f"sync_resume_{location}"):
            if not JOB.resume():
                st.warning(
                    "Import konnte nicht gestartet werden. Möglicherweise läuft bereits ein anderer Import."
                )
            st.rerun()


@st.fragment(run_every=5)
def render_sync_status(location):
    require_current_session()
    render_sync_feedback(location)


def render_sidebar_refresh(start, end):
    busy = JOB.snapshot().get("state") == "running"
    if st.button("🔄 aktualisieren", key="sync_sidebar_start", disabled=busy):
        request = make_sync_request(start, end, repair_sources=True, publish_raw=True)
        if not JOB.start(request):
            st.warning("Ein Datenimport läuft bereits. Bitte später erneut versuchen.")
    render_sync_status("sidebar")
