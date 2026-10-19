import json
import os
from datetime import time
from functools import lru_cache

import holidays
import numpy as np
import pandas as pd
from dotenv import load_dotenv
from jira import JIRA

from asset_country import attach_country, load_cache
from jira_loader import WORKSPACE_ID
from resolution_bands import HOURS_PER_WORKING_DAY, classify_bands

load_dotenv(override=True)

JIRA_URL = os.getenv("JIRA_URL")
JIRA_USERNAME = os.getenv("JIRA_USERNAME")
JIRA_PASSWORD = os.getenv("JIRA_PASSWORD")


@lru_cache(maxsize=1)
def get_jira():
    """Create the Jira client only when clone traversal actually needs it."""
    if not (JIRA_PASSWORD or "").strip():
        raise RuntimeError(
            "JIRA_PASSWORD is not set. Add it to .env before accessing Jira."
        )
    return JIRA(server=JIRA_URL, basic_auth=(JIRA_USERNAME, JIRA_PASSWORD))


# read json from data/jira-servicedesk-schema-objects.json
# with open('data/jira-servicedesk-schema-objects.json', 'r') as f:
#    schema = json.load(f)
# object_id_to_name = {v['id']: v['name'] for v in schema['values']}
BUS_START = time(8, 0)
BUS_END = time(18, 0)
TZ = "Europe/Berlin"


def enrich_jira_time_metrics(
    df: pd.DataFrame,
    tz: str = "Europe/Berlin",
    done_value: str = "Fertig",
    status_col: str = "currentstatus_name",
    created_col: str = "created",
    updated_col: str = "updated",
    status_date_col: str = "currentstatus_date",
    business_start: time = time(8, 0),
    business_end: time = time(18, 0),
    subdiv: (
        str | None
    ) = None,  # e.g. "BE", "BY", "NW", ...; None = federal holidays only
    hours_per_working_day: float = HOURS_PER_WORKING_DAY,
    bins: list[float] | None = None,
) -> pd.DataFrame:
    """
    Returns a copy of df with standardized timestamp columns and resolution KPIs.
    Requires: pip install holidays
    Expects: status_col contains "Fertig" for done tickets.
    """
    out = df.copy()

    # ---------- defaults ----------
    if bins is None:
        bins = [0, 1, 2, 4, 8, 24, 48, 72, 7 * 24, 14 * 24, 21 * 24]

    # ---------- parse timestamps ----------
    for c in [created_col, updated_col, status_date_col]:
        if c in out.columns:
            out[c] = pd.to_datetime(out[c], errors="coerce", utc=True).dt.tz_convert(tz)

    # ---------- done + resolved_at ----------
    if status_col not in out.columns:
        raise KeyError(f"Missing status column '{status_col}'")
    if created_col not in out.columns:
        raise KeyError(f"Missing created column '{created_col}'")
    if status_date_col not in out.columns:
        raise KeyError(f"Missing status date column '{status_date_col}'")

    out["is_done"] = out[status_col].astype("string") == done_value
    out["resolved_at"] = out[status_date_col].where(out["is_done"], pd.NaT)

    # ---------- simple elapsed hours ----------
    out["time_to_resolution_h"] = (
        out["resolved_at"] - out[created_col]
    ).dt.total_seconds() / 3600.0

    # ---------- calendar days (0 if same calendar day; only for done) ----------
    out["time_to_resolution_calendar_days"] = (
        out["resolved_at"].dt.tz_localize(None).dt.normalize()
        - out[created_col].dt.tz_localize(None).dt.normalize()
    ).dt.days
    out.loc[~out["is_done"], "time_to_resolution_calendar_days"] = np.nan

    out["resolution"] = np.where(
        out["is_done"],
        np.where(out["time_to_resolution_calendar_days"] < 1, "Same day", "> 1 day"),
        "Not done",
    )

    # ---------- holidays (Germany) ----------
    # Determine year range from created/resolved/updated (whatever is present)
    year_series = []
    for c in [created_col, updated_col, "resolved_at"]:
        if c in out.columns:
            year_series.append(out[c])
    years_stack = pd.concat(year_series, axis=0)

    # With no rows (or no parseable dates) .min()/.max() are NaN, and int(NaN)
    # raises "cannot convert float NaN to integer". Fall back to the current year.
    years = years_stack.dt.year.dropna()
    if years.empty:
        min_year = max_year = pd.Timestamp.today().year
    else:
        min_year = int(years.min())
        max_year = int(years.max())
    de = holidays.Germany(years=range(min_year, max_year + 1), subdiv=subdiv)

    holiday_set = set(de.keys())  # datetime.date for biz-hour logic
    holiday_dates = np.array(
        [np.datetime64(d) for d in de], dtype="datetime64[D]"
    )  # for np.busday_count

    # ---------- business hours helper ----------
    def business_seconds_between(start: pd.Timestamp, end: pd.Timestamp) -> float:
        if pd.isna(start) or pd.isna(end) or end <= start:
            return 0.0

        total = 0.0
        # iterate days in tz (timestamps already tz-aware)
        for day in pd.date_range(
            start.normalize(), end.normalize(), freq="D", tz=start.tz
        ):
            d = day.date()
            if day.weekday() >= 5 or d in holiday_set:
                continue

            day_start = pd.Timestamp.combine(d, business_start).tz_localize(start.tz)
            day_end = pd.Timestamp.combine(d, business_end).tz_localize(start.tz)

            a = max(start, day_start)
            b = min(end, day_end)
            if b > a:
                total += (b - a).total_seconds()
        return total

    # ---------- business hours/days to resolution ----------
    out["time_to_resolution_biz_hours"] = np.where(
        out["is_done"],
        out.apply(
            lambda r: (
                business_seconds_between(r[created_col], r["resolved_at"]) / 3600.0
            ),
            axis=1,
        ),
        np.nan,
    )
    out["time_to_resolution_biz_days"] = out["time_to_resolution_biz_hours"] / (
        (
            pd.Timestamp.combine(pd.Timestamp.today().date(), business_end)
            - pd.Timestamp.combine(pd.Timestamp.today().date(), business_start)
        ).total_seconds()
        / 3600.0
    )

    # ---------- resolution band (Länder tab) ----------
    out["resolution_band"] = classify_bands(
        out["time_to_resolution_biz_hours"],
        out["is_done"],
        hours_per_working_day=hours_per_working_day,
    )

    # ---------- bins on elapsed hours (real time) ----------
    labels = [f"{int(bins[i])}–{int(bins[i + 1])}" for i in range(len(bins) - 1)]
    out["time_to_resolution_bin"] = pd.cut(
        out["time_to_resolution_h"], bins=bins, labels=labels, include_lowest=True
    )

    # ---------- business day counts including holidays ----------
    # busday_count wants "datetime64[D]" without tz; normalize via tz_localize(None)
    created_days = out[created_col].dt.tz_localize(None).to_numpy(dtype="datetime64[D]")

    if updated_col in out.columns:
        updated_days = (
            out[updated_col].dt.tz_localize(None).to_numpy(dtype="datetime64[D]")
        )
        out["business_days_created_to_updated"] = np.busday_count(
            created_days, updated_days, weekmask="1111100", holidays=holiday_dates
        )
    else:
        out["business_days_created_to_updated"] = np.nan

    resolved_days = (
        out["resolved_at"].dt.tz_localize(None).to_numpy(dtype="datetime64[D]")
    )
    # For unresolved rows, busday_count will error if NaT -> convert to NaN afterwards
    # Workaround: compute only for done rows
    out["business_days_created_to_resolved"] = np.nan
    mask = out["is_done"] & out["resolved_at"].notna()
    out.loc[mask, "business_days_created_to_resolved"] = np.busday_count(
        created_days[mask.to_numpy()],
        resolved_days[mask.to_numpy()],
        weekmask="1111100",
        holidays=holiday_dates,
    )

    # ---------- helper strings ----------
    out["created_string"] = out[created_col].dt.strftime("%Y-%m-%d")
    if updated_col in out.columns:
        out["updated_string"] = out[updated_col].dt.strftime("%Y-%m-%d")
    else:
        out["updated_string"] = np.nan

    out["year"] = out[created_col].dt.year
    out["month"] = out[created_col].dt.month
    out["week_number"] = out[created_col].dt.isocalendar().week.astype(int)
    out["week_string"] = out[created_col].dt.strftime("%G-W%V")
    out["month_string"] = out[created_col].dt.strftime("%Y-%m")

    return out


"""
def business_seconds_between(start: pd.Timestamp,
                             end: pd.Timestamp,
                             holiday_set: set) -> float:
    if pd.isna(start) or pd.isna(end):
        return float("nan")
    if end <= start:
        return 0.0

    # iterate day by day, sum overlaps with business window
    total = 0.0
    for day in pd.date_range(start.normalize(), end.normalize(), freq="D", tz=start.tz):
        d = day.date()
        # weekday: Mon=0..Sun=6
        if day.weekday() >= 5 or d in holiday_set:
            continue

        day_start = pd.Timestamp.combine(d, BUS_START).tz_localize(start.tz)
        day_end   = pd.Timestamp.combine(d, BUS_END).tz_localize(start.tz)

        overlap_start = max(start, day_start)
        overlap_end   = min(end, day_end)
        if overlap_end > overlap_start:
            total += (overlap_end - overlap_start).total_seconds()
    return total"""

# read object_id_to_name from json file
with open("data/object_id_to_name.json", "r") as f:
    object_id_to_name = json.load(f)


def load_clone_of_clone(key):
    clones = []
    clone_types = []
    try:
        issue_raw = get_jira().issue(key).raw
        links = issue_raw["fields"].get("issuelinks", [])
        for link in links:
            if "outwardIssue" in link:
                clone_key = link["outwardIssue"]["key"]
                clone_type = link["type"]["name"]
                clones.append(clone_key)
                clone_types.append(clone_type)
    except Exception as e:  # noqa: BLE001 - preserve optional link traversal
        print(f"Error for clone_of_clone: {e!r}")
    return ",".join(clones), ",".join(clone_types)


def load_clones(links):
    clones = []
    clone_types = []
    clones_of_clones = []
    clone_types_of_clones = []
    try:
        for link in links:
            if "outwardIssue" in link:
                clone_key = link["outwardIssue"]["key"]
                clone_type = link["type"]["name"]
                clones.append(clone_key)
                clone_types.append(clone_type)
                cc, ctc = load_clone_of_clone(clone_key)
                clones_of_clones.append(cc)
                clone_types_of_clones.append(ctc)
    except Exception as e:  # noqa: BLE001 - preserve optional link traversal
        print(f"Error for clones: {e!r}")

    return (
        ",".join(clones),
        ",".join(clone_types),
        len(clones),
        ",".join(clones_of_clones),
        ",".join(clone_types_of_clones),
    )


_ISSUE_COLUMNS = [
    "key",
    "summary",
    "description",
    "status",
    "status_category",
    "created",
    "updated",
    "labels",
    "source",
    "priority",
    "category",
    "issuetype",
    "main_category_id",
    "sub_category_id",
    "currentstatus_name",
    "currentstatus_date",
    "comments",
    "request_type",
    "clones",
    "clone_types",
    "cloned_by",
    "n_clones",
    "ansprechpartner",
    "zentrale",
    "filiale",
    "Link",
    "clones_of_clones",
    "clone_types_of_clones",
]


def _nested(mapping, *keys, default=""):
    value = mapping
    for key in keys:
        if not isinstance(value, dict):
            return default
        value = value.get(key)
        if value is None:
            return default
    return value


def _asset_id(fields, field_name):
    values = fields.get(field_name) or []
    if not isinstance(values, list) or not values or not isinstance(values[0], dict):
        return ""
    value = values[0].get("objectId")
    return "" if value is None else str(value)


def _escalation_asset_id(fields, field_name):
    """objectId of a country-escalation field, but only from the normal workspace.

    Country resolution keys its cache by objectId alone, so an objectId minted
    in another Assets workspace would collide with a normal-workspace entry and
    hand the ticket an unrelated site's country - and because Ansprechpartner
    outranks Filiale, a foreign id can override a country that was correct.
    jira_country_export._object_id_for_field() rejects the same references; this
    keeps the dashboard path consistent with it.

    A reference without a workspaceId at all is legacy raw data, not a
    cross-workspace reference, so it is accepted - the same rule _asset_label()
    applies to inputs that predate workspace tagging.
    """
    values = fields.get(field_name) or []
    if not isinstance(values, list) or not values or not isinstance(values[0], dict):
        return ""
    workspace = values[0].get("workspaceId")
    if workspace is not None and workspace != WORKSPACE_ID:
        return ""
    return _asset_id(fields, field_name)


def _escalation_cell(fields, field_name):
    """The stored 'ID_<objectId>' cell for an escalation field, or ''."""
    object_id = _escalation_asset_id(fields, field_name)
    return f"ID_{object_id}" if object_id else ""


def _asset_label(issue, field_name):
    if (issue.get("category_status") or {}).get(field_name) == "unassigned":
        return "Nicht zugeordnet"
    fields = issue.get("fields") or {}
    object_id = _asset_id(fields, field_name)
    normal_marker = any(
        name in issue
        for name in ("asset_labels", "assets_workspace_id", "assets_cloud_id")
    )
    if normal_marker and object_id:
        reference = fields[field_name][0]
        if reference.get("workspaceId") != WORKSPACE_ID:
            return "Unbekannt"
    labels = issue.get("asset_labels") or {}
    if object_id and str(object_id) in labels and labels[str(object_id)]:
        return str(labels[str(object_id)])
    if normal_marker:
        return "Unbekannt"
    return object_id_to_name.get(str(object_id), "Unbekannt")


def _extract_issue(issue, comment_separator):
    fields = issue.get("fields") or {}
    links = fields.get("issuelinks") or []
    if links:
        clones, clone_types, n_clones, clones_of_clones, clone_types_of_clones = (
            load_clones(links)
        )
    else:
        clones = clone_types = clones_of_clones = clone_types_of_clones = ""
        n_clones = 0

    comments = _nested(fields, "comment", "comments", default=[]) or []
    comment_text = comment_separator.join(
        str(comment.get("body", ""))
        for comment in comments
        if isinstance(comment, dict)
    )
    main_id = _asset_id(fields, "customfield_10680")
    sub_id = _asset_id(fields, "customfield_10679")
    cloned_by = _nested(links[0], "inwardIssue", "key") if links else ""

    return {
        "key": issue.get("key", ""),
        "summary": fields.get("summary") or "",
        "description": fields.get("description") or "",
        "status": _nested(fields, "status", "name"),
        "status_category": _nested(fields, "status", "statusCategory", "name"),
        "created": fields.get("created") or "",
        "updated": fields.get("updated") or "",
        "labels": fields.get("labels") or [],
        "source": _nested(fields, "customfield_10675", "value"),
        "priority": _nested(fields, "priority", "name"),
        "category": fields.get("customfield_10065") or [],
        "issuetype": _nested(fields, "issuetype", "name"),
        "main_category_id": main_id,
        "sub_category_id": sub_id,
        "currentstatus_name": _nested(
            fields, "customfield_10010", "currentStatus", "status"
        ),
        "currentstatus_date": _nested(
            fields, "customfield_10010", "currentStatus", "statusDate", "jira"
        ),
        "comments": comment_text,
        "request_type": _nested(fields, "customfield_10010", "requestType", "name"),
        "clones": clones,
        "clone_types": clone_types,
        "cloned_by": cloned_by,
        "n_clones": n_clones,
        "ansprechpartner": _escalation_cell(fields, "customfield_10689"),
        "zentrale": _escalation_cell(fields, "customfield_10673"),
        "filiale": _escalation_cell(fields, "customfield_10674"),
        "Link": _nested(fields, "customfield_10010", "_links", "agent"),
        "clones_of_clones": clones_of_clones,
        "clone_types_of_clones": clone_types_of_clones,
        "Hauptkategorie": _asset_label(issue, "customfield_10680"),
        "Unterkategorie": _asset_label(issue, "customfield_10679"),
    }


def _load_service_desk_issues(
    issues,
    *,
    firma,
    clone_project=None,
    clone_of_clone_project=None,
    comment_separator="\n--------------------------------\n",
):
    rows = [_extract_issue(issue, comment_separator) for issue in (issues or [])]
    df = pd.DataFrame(
        rows, columns=[*_ISSUE_COLUMNS, "Hauptkategorie", "Unterkategorie"]
    )
    for column in ("created", "updated", "currentstatus_date"):
        df[column] = pd.to_datetime(
            df[column], errors="coerce", utc=True
        ).dt.tz_convert(TZ)

    df = enrich_jira_time_metrics(df, subdiv="BW")
    df["ansprechpartner"] = df["ansprechpartner"].astype(str)
    df["zentrale"] = df["zentrale"].astype(str)
    df["filiale"] = df["filiale"].astype(str)
    # Land alongside resolution_band, so a refreshed ticket is charted in the
    # Länder tab instead of being dropped by its groupby as a NaN key. This is
    # a pure cache lookup - no Assets API call, no credentials. Ids the cache
    # has not seen become NOT_RESOLVED rather than silently claiming the asset
    # carries no country; backfill_country.py resolves them.
    df = attach_country(df, load_cache())
    df["firma"] = firma
    df["clone_in_project"] = df["clones"].apply(
        lambda value: (
            ",".join(key.split("-")[0] for key in value.split(",")) if value else "-"
        )
    )
    df["has_exax_clone"] = (
        df["clone_in_project"].str.contains(clone_project, regex=False)
        if clone_project
        else False
    )
    df["has_axt_clone_clone"] = (
        df["clones_of_clones"].str.contains(clone_of_clone_project, regex=False)
        if clone_of_clone_project
        else False
    )
    for column in ("has_exax_clone", "has_axt_clone_clone"):
        df[column] = df[column].fillna(False).astype(bool)
    return df[["firma", *[column for column in df.columns if column != "firma"]]]


def load_issues(issues):
    return _load_service_desk_issues(
        issues,
        firma="IPRO",
        clone_project="EXIPR",
        comment_separator="\n\n",
    )


def load_issues_Amparex(issues):
    return _load_service_desk_issues(
        issues,
        firma="Amparex",
        clone_project="EXAX",
        clone_of_clone_project="AX",
    )


def load_issues_Euronet(issues):
    return _load_service_desk_issues(issues, firma="Euronet")


def load_project_issues(project, issues):
    project_key = str(project).upper()
    loaders = {
        "SDIPR": load_issues,
        "SDAX": load_issues_Amparex,
        "SDEU": load_issues_Euronet,
    }
    try:
        loader = loaders[project_key]
    except KeyError as exc:
        raise ValueError(f"Unsupported Jira project: {project}") from exc
    return loader(issues)


def _normalize_bool_columns(df):
    for column in ("is_done", "has_exax_clone", "has_axt_clone_clone"):
        if column not in df.columns:
            continue
        if df[column].isna().any():
            df[column] = df[column].astype("boolean")
        else:
            df[column] = df[column].astype(bool)
    return df


def upsert_jira_data(df_old, df_new, key_col="key"):
    old = df_old.copy()
    new = df_new.copy()

    if key_col not in new.columns:
        if new.empty:
            return _normalize_bool_columns(old)
        raise KeyError(f"Missing key column '{key_col}' in refreshed Jira data")
    new = new.loc[new[key_col].notna()].drop_duplicates(key_col, keep="last")

    if old.empty and key_col not in old.columns:
        return _normalize_bool_columns(new.reset_index(drop=True))
    if key_col not in old.columns:
        raise KeyError(f"Missing key column '{key_col}' in cached Jira data")
    old = old.loc[old[key_col].notna()].drop_duplicates(key_col, keep="last")
    if new.empty:
        return _normalize_bool_columns(old.reset_index(drop=True))

    old = old.set_index(key_col)
    new = new.set_index(key_col)
    all_columns = old.columns.union(new.columns, sort=False)
    old = old.reindex(columns=all_columns)
    new = new.reindex(columns=all_columns)

    shared_keys = old.index.intersection(new.index)
    # Columns present in the refresh are authoritative, including explicit nulls.
    for column in df_new.columns:
        if column != key_col:
            old.loc[shared_keys, column] = new.loc[shared_keys, column]
    inserted = new.loc[new.index.difference(old.index)]
    combined = pd.concat([old, inserted], axis=0).reset_index()
    return _normalize_bool_columns(combined)
