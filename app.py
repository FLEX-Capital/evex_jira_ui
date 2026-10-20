from datetime import datetime, timedelta

import pandas as pd
import streamlit as st

from cache_schema import CacheReadError, CacheSchemaError
from dashboard_views import DashboardContext, render_dashboard
from data_loading import load_data_snapshot
from data_transformation import enrich_jira_time_metrics
from maintenance_ui import render_import_empty_state
from service_desks import (
    BERLIN,
    COMPANY_LABELS,
    EARLIEST_DAY,
    berlin_day_end,
    berlin_day_start,
    filter_companies,
)
from settings import SettingsError, load_settings
from styles import CUSTOM_CSS
from sync_jobs import render_sidebar_refresh
from ui_auth import require_password

# set to dark mode
st.set_page_config(page_title="Jira Analytics Dashboard", layout="wide")
# Apply custom CSS styles
st.markdown(CUSTOM_CSS, unsafe_allow_html=True)


# Sidebar
# add logo in sidebar
settings = load_settings()
paths = settings.paths
st.sidebar.image(str(paths.logo), width=200)
st.sidebar.header("\n\nJIRA Data Analysis")

# Require password before showing any data/controls (set UI_PASSWORD to enable).
require_password()

st.sidebar.subheader("Data Controls")
selected_companies = st.sidebar.multiselect("Firma", COMPANY_LABELS, default=COMPANY_LABELS)

cache_revision = None
try:
    df_old, cache_revision = load_data_snapshot(path=paths.tickets)
except (CacheReadError, CacheSchemaError, SettingsError, OSError):
    st.error(
        "Cache konnte nicht geladen werden. Bitte Schema, Datenverzeichnis und Tenant prüfen "
        "oder eine passende Sicherung wiederherstellen."
    )
    st.stop()
df_old = df_old if df_old is not None else pd.DataFrame()
df = df_old.copy()

st.session_state["_sync_seen_revision"] = cache_revision

# The whole dashboard reports in Berlin local time (service_desks.BERLIN): `created`
# is stored as Europe/Berlin (data_transformation.TZ), and the service desk works
# Berlin hours.

# Optional global filters in sidebar.
# `now` must be Berlin-local, not UTC: between 00:00 and 02:00 Berlin the UTC date
# is still the previous day, which silently shifted the default window back a day.
start_date = (datetime.now(BERLIN) - timedelta(days=7)).date()
end_date = datetime.now(BERLIN).date()
picked = st.sidebar.date_input(
    "Zeitraum (erstellt)", value=(start_date, end_date), min_value=EARLIEST_DAY
)

# While a range is being picked, Streamlit reruns after the FIRST click and
# returns a 1-tuple; unpacking that straight into two names raised
# "not enough values to unpack". Keep the last complete range until the
# second date is chosen, so the charts stay put instead of erroring.
if len(picked) == 2:
    start_date, end_date = picked
    st.session_state["date_range"] = (start_date, end_date)
else:
    start_date, end_date = st.session_state.get("date_range", (start_date, end_date))
    st.sidebar.info("Bitte Enddatum wählen.")

# The picked day must span 00:00-23:59:59 Berlin. Building these in UTC shifted the
# window by 1-2h, so tickets created just after Berlin midnight fell outside it.
start_dt, end_dt = berlin_day_start(start_date), berlin_day_end(end_date)

# toggle to switch between week_string and created_string
if st.sidebar.toggle("Auf Wochenbasis"):
    x_axis = "week_string"
    x_axis_label = "Kalenderwoche"
else:
    x_axis = "created_string"
    x_axis_label = "Datum"

st.sidebar.write("JIRA Daten aktualisieren.")

with st.sidebar:
    render_sidebar_refresh(start_dt, end_dt)

if not selected_companies:
    render_import_empty_state("Bitte mindestens eine Firma auswählen.")
    st.stop()
if df is None or df.empty:
    render_import_empty_state(
        "Keine Jira-Daten vorhanden. Bitte über die Seitenleiste aktualisieren."
    )
    st.stop()

created = pd.to_datetime(df["created"], errors="coerce", utc=True)
df = df.loc[(created >= start_dt) & (created <= end_dt)]
df = filter_companies(df, selected_companies)
st.sidebar.info(f"{len(df)} Tickets für die gewählten Firmen im Zeitraum.")
if df.empty:
    render_import_empty_state("Keine Daten für die gewählten Firmen im Zeitraum.")
    st.stop()
if "completion_state" not in df or df["completion_state"].isna().any():
    df = enrich_jira_time_metrics(df)
for group_column in ("Hauptkategorie", "status_category", "status", "zentrale"):
    if group_column in df:
        df[group_column] = (
            df[group_column].astype("string").fillna("").str.strip().replace("", "Unbekannt")
        )
completion_counts = df["completion_state"].value_counts()
st.caption(
    f"Abgeschlossen: {completion_counts.get('done', 0)}; "
    f"Offen: {completion_counts.get('open', 0)}; "
    f"Unbekannter Abschlussstatus: {completion_counts.get('unknown', 0)}"
)
render_dashboard(DashboardContext(df, df_old, cache_revision, settings, x_axis, x_axis_label))
