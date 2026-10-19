"""Removable custom-date import controls; execution belongs to the shared service."""

from datetime import datetime

import streamlit as st

import sync_jobs
from desk_sync import make_sync_request
from service_desks import BERLIN, BY_KEY, EARLIEST_DAY, berlin_day_end, berlin_day_start

START = datetime(2025, 1, 1, tzinfo=BERLIN)
DATES_KEY = "temporary_history_dates"
REJECTED_KEY = "_history_rejected"


def _follow_today(today):
    """Keep a default end of "today" current in tabs left open past midnight.

    A keyed date_input keeps its stored value when its default changes, so the
    range is seeded in session state and an end on the previous "today" moves on.
    """
    previous = st.session_state.get("_history_today")
    st.session_state["_history_today"] = today
    picked = st.session_state.get(DATES_KEY)
    if picked is None:
        st.session_state[DATES_KEY] = (START.date(), today)
    elif previous not in (None, today) and len(picked) == 2 and picked[1] == previous:
        st.session_state[DATES_KEY] = (picked[0], today)


@st.fragment(run_every=5)
def render_history_pull():
    with st.container():
        st.subheader("Backfill / Datenimport")
        st.caption(
            "Lädt Tickets, die im gewählten Zeitraum erstellt wurden, für die gewählten Service Desks. "
            "Beide Tage zählen vollständig (Berlin); heute endet am Importstart. "
            "Diese Auswahl ist unabhängig von den Filtern in der Seitenleiste."
        )
        st.caption(
            "Vorhandene Tickets werden über ihren Jira-Schlüssel aktualisiert, neue ergänzt. "
            "Ältere Tickets und parallele Änderungen bleiben erhalten. Unbekannte Kategorien werden automatisch ergänzt."
        )
        busy = sync_jobs.JOB.snapshot().get("state") == "running"
        now = datetime.now(BERLIN)
        _follow_today(now.date())
        picked = st.date_input(
            "Backfill-Zeitraum (erstellt)",
            min_value=EARLIEST_DAY,
            max_value=now.date(),
            format="DD.MM.YYYY",
            disabled=busy,
            key=DATES_KEY,
        )
        projects = st.multiselect(
            "Service Desks",
            options=list(BY_KEY),
            default=list(BY_KEY),
            format_func=lambda project: f"{BY_KEY[project].label} ({project})",
            disabled=busy,
            key="temporary_history_desks",
        )
        ordered = len(picked) == 2 and picked[0] <= picked[1]
        if len(picked) != 2:
            st.info("Bitte Start- und Enddatum für den Import wählen.")
        elif not ordered:
            st.warning("Das Startdatum muss vor oder auf dem Enddatum liegen.")
        if not projects:
            st.info("Bitte mindestens einen Service Desk für den Import wählen.")
        if REJECTED_KEY in st.session_state:
            st.warning(st.session_state[REJECTED_KEY])
        if st.button(
            "📥 Ausgewählte Tickets laden",
            disabled=busy or not ordered or not projects,
            key="temporary_history_start",
        ):
            request = make_sync_request(
                berlin_day_start(picked[0]),
                min(berlin_day_end(picked[1]), now),
                projects,
            )
            if sync_jobs.JOB.start(request):
                st.session_state.pop(REJECTED_KEY, None)
            else:
                st.session_state[REJECTED_KEY] = (
                    "Ein Datenimport läuft bereits oder konnte nicht gestartet werden. Bitte später erneut versuchen."
                )
            st.rerun()
        sync_jobs.render_sync_feedback("custom")
