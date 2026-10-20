import json
from collections.abc import Mapping
from dataclasses import asdict
from datetime import time
from typing import Any

import holidays
import numpy as np
import pandas as pd
from jira import JIRA

import reporting_policy as reporting
from asset_country import attach_country, load_cache
from issue_links import clone_links, parse_issue_links
from jira_loader import WORKSPACE_ID
from resolution_bands import classify_bands
from settings import load_settings


def get_jira() -> JIRA:
    """Create a Jira client from the current requested-operation settings."""
    connection = load_settings().require_jira()
    return JIRA(
        server=connection.url, basic_auth=(connection.username, connection.password)
    )


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
    done_value: str = reporting.LEGACY_DONE,
    status_col: str = "currentstatus_name",
    created_col: str = "created",
    updated_col: str = "updated",
    status_date_col: str = "currentstatus_date",
    business_start: time = reporting.BUSINESS_START,
    business_end: time = reporting.BUSINESS_END,
    subdiv: str | None = reporting.HOLIDAY_SUBDIV,
    hours_per_working_day: float | None = None,
    bins: list[float] | None = None,
) -> pd.DataFrame:
    """Normalize completion and calculate metrics only for valid time intervals.

    Args:
        df: Ticket records; input is never modified.
        tz: Reporting timezone.
        done_value: Legacy completed-status label when category identity is absent.
        status_col: Legacy status label column.
        created_col: Creation timestamp column.
        updated_col: Update timestamp column.
        status_date_col: Resolution/status-change timestamp column.
        business_start: Local start of each weekday work window.
        business_end: Local end of each weekday work window.
        subdiv: German holiday subdivision, or federal holidays when None.
        hours_per_working_day: Optional explicit band threshold.
        bins: Optional elapsed-hour interval edges.

    Returns:
        A copy with normalized states, quality markers and nullable metrics.
    """
    out = df.copy()
    bins = list(reporting.ELAPSED_EDGES) if bins is None else bins
    window_hours = (
        pd.Timestamp.combine(pd.Timestamp.min.date(), business_end)
        - pd.Timestamp.combine(pd.Timestamp.min.date(), business_start)
    ).total_seconds() / 3600
    if window_hours <= 0:
        raise ValueError("Business end must be after business start")
    band_hours = (
        window_hours if hours_per_working_day is None else hours_per_working_day
    )
    for column in (created_col, updated_col, status_date_col):
        values = out.get(column, pd.Series(index=out.index, dtype="object"))
        out[column] = pd.to_datetime(
            values, errors="coerce", utc=True, format="mixed"
        ).dt.tz_convert(tz)
    keys = out.get("status_category_key", pd.Series(index=out.index, dtype="object"))
    statuses = out.get(status_col, pd.Series(index=out.index, dtype="object"))
    out["status_category_key"] = keys
    out["completion_state"] = pd.Series(
        [
            reporting.completion_state(key, status, done_value=done_value)
            for key, status in zip(keys, statuses, strict=True)
        ],
        index=out.index,
        dtype="string",
    )
    out["is_done"] = (
        out["completion_state"].map({"done": True, "open": False}).astype("boolean")
    )
    done = out["completion_state"].eq("done")
    out["resolved_at"] = out[status_date_col].where(done, pd.NaT)
    endpoints = out[created_col].notna() & out["resolved_at"].notna()
    reversed_time = endpoints & (out["resolved_at"] < out[created_col])
    valid = done & endpoints & ~reversed_time
    out["duration_quality"] = pd.Series("missing", index=out.index, dtype="string")
    out.loc[reversed_time, "duration_quality"] = "reversed"
    out.loc[valid, "duration_quality"] = "valid"
    out["time_to_resolution_h"] = (
        (out["resolved_at"] - out[created_col]).dt.total_seconds() / 3600
    ).where(valid)
    out["time_to_resolution_calendar_days"] = (
        (
            out["resolved_at"].dt.tz_localize(None).dt.normalize()
            - out[created_col].dt.tz_localize(None).dt.normalize()
        )
        .dt.days.where(valid)
        .astype("float64")
    )
    out["resolution"] = "Not done"
    out.loc[out["completion_state"].eq("unknown"), "resolution"] = (
        "Unbekannter Abschlussstatus"
    )
    out.loc[done & ~valid, "resolution"] = "Unbekannte Dauer"
    out.loc[valid, "resolution"] = np.where(
        out.loc[valid, "time_to_resolution_calendar_days"] < 1, "Same day", "> 1 day"
    )
    dates = pd.concat([out[created_col], out[updated_col], out["resolved_at"]])
    years = dates.dt.year.dropna()
    min_year = int(years.min()) if not years.empty else pd.Timestamp.now(tz=tz).year
    max_year = int(years.max()) if not years.empty else min_year
    calendar = holidays.Germany(years=range(min_year, max_year + 1), subdiv=subdiv)
    holiday_dates = np.array(
        [np.datetime64(day) for day in calendar], dtype="datetime64[D]"
    )

    def business_hours(start: pd.Timestamp, end: pd.Timestamp) -> float:
        total = 0.0
        for day in pd.date_range(start.normalize(), end.normalize(), freq="D"):
            if day.weekday() >= 5 or day.date() in calendar:
                continue
            lower = pd.Timestamp.combine(day.date(), business_start).tz_localize(tz)
            upper = pd.Timestamp.combine(day.date(), business_end).tz_localize(tz)
            overlap = min(end, upper) - max(start, lower)
            total += max(0, overlap.total_seconds()) / 3600
        return total

    out["time_to_resolution_biz_hours"] = pd.Series(
        [
            business_hours(start, end) if good else np.nan
            for start, end, good in zip(
                out[created_col], out["resolved_at"], valid, strict=True
            )
        ],
        index=out.index,
        dtype="float64",
    )
    out["time_to_resolution_biz_days"] = (
        out["time_to_resolution_biz_hours"] / window_hours
    )
    out["resolution_band"] = classify_bands(
        out["time_to_resolution_biz_hours"],
        out["is_done"],
        hours_per_working_day=band_hours,
    )
    labels = [f"{int(bins[i])}–{int(bins[i + 1])}" for i in range(len(bins) - 1)]
    overflow_label = f"> {int(bins[-1])}"
    duration_labels = pd.cut(
        out["time_to_resolution_h"], bins=bins, labels=labels, include_lowest=True
    ).astype("string")
    duration_labels = duration_labels.mask(
        out["time_to_resolution_h"] > bins[-1], overflow_label
    )
    duration_labels = duration_labels.fillna("Unbekannte Dauer")
    duration_labels = duration_labels.mask(
        out["completion_state"] == "open", "Nicht abgeschlossen"
    )
    duration_labels = duration_labels.mask(
        out["completion_state"] == "unknown", "Unbekannter Abschlussstatus"
    )
    out["time_to_resolution_bin"] = pd.Categorical(
        duration_labels,
        categories=[
            *labels,
            overflow_label,
            "Unbekannte Dauer",
            "Nicht abgeschlossen",
            "Unbekannter Abschlussstatus",
        ],
        ordered=True,
    )
    created_days = out[created_col].dt.tz_localize(None).to_numpy(dtype="datetime64[D]")
    for target, metric in [
        (updated_col, "business_days_created_to_updated"),
        ("resolved_at", "business_days_created_to_resolved"),
    ]:
        mask = (
            out[created_col].notna()
            & out[target].notna()
            & (out[target] >= out[created_col])
        )
        out[metric] = np.nan
        target_days = out[target].dt.tz_localize(None).to_numpy(dtype="datetime64[D]")
        out.loc[mask, metric] = np.busday_count(
            created_days[mask.to_numpy()],
            target_days[mask.to_numpy()],
            weekmask="1111100",
            holidays=holiday_dates,
        )
        if target == updated_col and mask.all():
            out[metric] = out[metric].astype("int64")
    out["created_string"] = out[created_col].dt.strftime("%Y-%m-%d")
    out["updated_string"] = out[updated_col].dt.strftime("%Y-%m-%d")
    out["year"] = out[created_col].dt.year
    out["month"] = out[created_col].dt.month
    weeks = out[created_col].dt.isocalendar().week
    out["week_number"] = weeks.astype("int64" if weeks.notna().all() else "Int64")
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
with load_settings().paths.category_map.open() as f:
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
    "status_category_key",
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
    "issue_links",
    "clone_count",
    "clone_inward_keys",
    "clone_outward_keys",
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
    normalized = parse_issue_links(links)
    clones_only = clone_links(normalized)
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
        "status_category_key": _nested(fields, "status", "statusCategory", "key"),
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
        "issue_links": [asdict(link) for link in normalized],
        "clone_count": len({link.target_key for link in clones_only}),
        "clone_inward_keys": [
            link.target_key for link in clones_only if link.direction == "inward"
        ],
        "clone_outward_keys": [
            link.target_key for link in clones_only if link.direction == "outward"
        ],
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
    country_cache=None,
):
    rows = [_extract_issue(issue, comment_separator) for issue in (issues or [])]
    df = pd.DataFrame(
        rows, columns=[*_ISSUE_COLUMNS, "Hauptkategorie", "Unterkategorie"]
    )
    for column in ("created", "updated", "currentstatus_date"):
        df[column] = pd.to_datetime(
            df[column], errors="coerce", utc=True, format="mixed"
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
    df = attach_country(df, load_cache() if country_cache is None else country_cache)
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


def load_issues(
    issues: list[dict[str, Any]] | None,
    *,
    country_cache: Mapping[str, str | None] | None = None,
) -> pd.DataFrame:
    """Transform this desk using supplied country evidence when available."""
    return _load_service_desk_issues(
        issues,
        firma="IPRO",
        country_cache=country_cache,
        clone_project="EXIPR",
        comment_separator="\n\n",
    )


def load_issues_Amparex(
    issues: list[dict[str, Any]] | None,
    *,
    country_cache: Mapping[str, str | None] | None = None,
) -> pd.DataFrame:
    """Transform this desk using supplied country evidence when available."""
    return _load_service_desk_issues(
        issues,
        firma="Amparex",
        country_cache=country_cache,
        clone_project="EXAX",
        clone_of_clone_project="AX",
    )


def load_issues_Euronet(
    issues: list[dict[str, Any]] | None,
    *,
    country_cache: Mapping[str, str | None] | None = None,
) -> pd.DataFrame:
    """Transform this desk using supplied country evidence when available."""
    return _load_service_desk_issues(
        issues, firma="Euronet", country_cache=country_cache
    )


def load_project_issues(
    project: str,
    issues: list[dict[str, Any]] | None,
    *,
    country_cache: Mapping[str, str | None] | None = None,
) -> pd.DataFrame:
    """Dispatch desk transformation with explicitly captured country evidence."""
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
    return loader(issues, country_cache=country_cache)


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
