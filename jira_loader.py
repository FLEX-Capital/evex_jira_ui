import json
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from enum import Enum
from functools import cache, partial
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests
from jira import JIRA

import service_desks as desks
from service_desks import InvalidIssue, as_utc, effective_start, filter_issue_window
from settings import (
    DEFAULT_CLOUD_ID,
    DEFAULT_WORKSPACE_ID,
    JiraConnection,
    Settings,
    load_settings,
)

CLOUD_ID = DEFAULT_CLOUD_ID

WORKSPACE_ID = DEFAULT_WORKSPACE_ID


def _require_credentials(settings: Settings | None = None) -> JiraConnection:
    return (settings or load_settings()).require_jira()


def jira_request(url, params=None, timeout=30, *, settings=None):
    """Helper function to make authenticated requests to the JIRA API."""
    connection = _require_credentials(settings)
    response = requests.get(
        url,
        auth=(connection.username, connection.password),
        headers={"Accept": "application/json"},
        params=params,
        timeout=timeout,
    )
    response.raise_for_status()
    return response.json()


@cache
def _cached_asset(
    cloud_id: str, workspace_id: str, object_id: str | None, settings: Settings
) -> dict[str, Any]:
    return fetch_asset_object_uncached(
        cloud_id, workspace_id, object_id, settings=settings
    )


def fetch_asset_object(
    cloud_id: str | None = None,
    workspace_id: str | None = None,
    object_id: str | None = None,
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Cache a successful Assets lookup by full tenant and settings snapshot."""
    config = settings or load_settings()
    cloud_id = config.tenant.cloud_id if cloud_id is None else cloud_id
    workspace_id = config.tenant.workspace_id if workspace_id is None else workspace_id
    return _cached_asset(cloud_id, workspace_id, object_id, config)


# Preserve the existing explicit cache-management adapter.
setattr(fetch_asset_object, "cache_clear", _cached_asset.cache_clear)


def fetch_asset_object_uncached(
    cloud_id: str,
    workspace_id: str,
    object_id: str | None,
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Read an Assets object under one captured credential snapshot."""
    url = f"https://api.atlassian.com/ex/jira/{cloud_id}/jsm/assets/workspace/{workspace_id}/v1/object/{object_id}"
    return jira_request(url, settings=settings)


def get_workspace_id(workspace_name, *, settings=None):
    """Fetch the workspace ID for a given workspace name."""
    config = settings or load_settings()
    url = f"https://api.atlassian.com/ex/jira/{config.tenant.cloud_id}/jsm/assets/workspace/list"
    workspaces = jira_request(url, settings=config).get("values", [])
    for ws in workspaces:
        if ws.get("name", "").lower() == workspace_name.lower():
            return ws.get("id")
    raise ValueError(f"Workspace '{workspace_name}' not found")


def get_workspaces(*, settings=None):
    # https://<Assets Site Name>.atlassian.net/rest/servicedeskapi/assets/workspace
    config = settings or load_settings()
    url = f"{config.require_jira().url}/rest/servicedeskapi/assets/workspace"
    return jira_request(url, settings=config)


def fetch_object_schema_list(
    start_at=0,
    max_results=50,
    include_counts=False,
    workspace_id=None,
    *,
    settings=None,
):
    """Fetch a page of Jira Assets object schemas via the Atlassian Cloud Assets REST API."""
    config = settings or load_settings()
    workspace_id = config.tenant.workspace_id if workspace_id is None else workspace_id
    url = (
        f"https://api.atlassian.com/ex/jira/{config.tenant.cloud_id}/jsm/assets/workspace/"
        f"{workspace_id}/v1/objectschema/list"
    )
    return jira_request(
        url,
        params={
            "startAt": start_at,
            "maxResults": max_results,
            "includeCounts": str(include_counts).lower(),
        },
        timeout=30,
        settings=config,
    )


def fetch_all_object_schemas(
    workspace_id=None, max_results=50, include_counts=False, *, settings=None
):
    """Fetch all Jira Assets object schemas, paging through the list endpoint."""
    config = settings or load_settings()
    schemas = []
    start_at = 0
    while True:
        page = fetch_object_schema_list(
            workspace_id=workspace_id,
            start_at=start_at,
            max_results=max_results,
            include_counts=include_counts,
            settings=config,
        )
        values = page.get("values", [])
        schemas.extend(values)
        if not values or start_at + len(values) >= page.get("total", 0):
            break
        start_at += max_results
    return schemas


def get_asset_attribute(asset, attribute_name):
    """Return the displayValue of a named attribute on an Asset object response."""
    for attr in asset.get("attributes", []):
        name = attr.get("objectTypeAttribute", {}).get("name", "")
        if name.lower() == attribute_name.lower():
            for value in attr.get("objectAttributeValues", []):
                display = value.get("displayValue") or value.get("value")
                if display:
                    return display
    return None


# The fields anything downstream actually reads: data_transformation._extract_issue
# (the bulk), enrich_issue_assets (the customfield_106xx asset refs),
# filter_issue_window (created) and dedupe_issues (updated).
#
# A search response carries ~170 fields per issue. Keeping only these cuts an
# issue from ~15 KB to ~5 KB, which is what lets a full backfill finish without
# exhausting memory. Trimming happens client-side rather than by narrowing the
# Jira query, so a mistake here costs a field we already had rather than one we
# never fetched. tests/test_assets_and_fetch.py pins this set to what the
# transformation reads, so adding a field there fails loudly here.
RETAINED_FIELDS = frozenset(desks.FIELDS.retained())


def trim_issue(issue):
    """Drop the ~150 fields per issue that nothing downstream reads."""
    trimmed = {key: value for key, value in issue.items() if key != "fields"}
    trimmed["fields"] = {
        name: value
        for name, value in (issue.get("fields") or {}).items()
        if name in desks.FIELDS.retained()
    }
    return trimmed


ASSET_FIELDS = (
    desks.FIELDS.main_category,
    desks.FIELDS.sub_category,
    desks.FIELDS.central,
    desks.FIELDS.branch,
)


def enrich_issue_assets(issue, *, category_resolver=None, settings=None):
    """Resolve only normal-workspace references; retain explicit optional failures."""
    config = settings or load_settings()
    tenant = config.tenant
    result = dict(issue)
    result.pop("customer", None)
    result["customer_country"] = None
    result["asset_labels"] = {}
    result["asset_errors"] = []
    result["assets_cloud_id"] = tenant.cloud_id
    result["assets_workspace_id"] = tenant.workspace_id
    # Explicit contexts span all projects and the later cache-repair pass.
    from category_sync import CategoryResolver

    category_resolver = category_resolver or CategoryResolver(
        fetch=partial(fetch_asset_object, settings=config), settings=config
    )
    category_fields = {item[0] for item in desks.category_fields()}
    result["category_status"] = {}
    for field in (
        desks.FIELDS.main_category,
        desks.FIELDS.sub_category,
        desks.FIELDS.central,
        desks.FIELDS.branch,
    ):
        if (
            field in category_fields
            and field in (issue.get("fields") or {})
            and not issue["fields"][field]
        ):
            result["category_status"][field] = "unassigned"
        for ref in issue.get("fields", {}).get(field) or []:
            object_id = str(ref.get("objectId", ""))
            workspace = ref.get("workspaceId")
            if not object_id or workspace != tenant.workspace_id:
                result["asset_errors"].append(
                    f"{field}: unresolved workspace reference {object_id}"
                )
                continue
            if field in category_fields:
                resolved = category_resolver.resolve(
                    tenant.cloud_id, tenant.workspace_id, object_id
                )
                if resolved.label:
                    result["asset_labels"][object_id] = resolved.label
                    result["category_status"][field] = "resolved"
                else:
                    result["category_status"][field] = "unresolved"
                    result["asset_errors"].append(
                        f"{field}/{object_id}: {resolved.error}"
                    )
                continue
            try:
                asset = fetch_asset_object(
                    tenant.cloud_id, tenant.workspace_id, object_id, settings=config
                )
                if str(asset.get("id")) != object_id:
                    raise ValueError("Assets returned a different object identity")
                label = asset.get("label") or asset.get("name")
                if label:
                    result["asset_labels"][object_id] = label
                if field == desks.FIELDS.central:
                    # Keep the country, discard the Assets object it came from.
                    # That object averaged 24 KB per issue - 63% of the whole
                    # payload - and nothing downstream ever read it, so holding
                    # it was what pushed a full backfill out of memory.
                    result["customer_country"] = get_asset_attribute(
                        asset, "Land"
                    ) or get_asset_attribute(asset, "Country")
            except (requests.RequestException, PermissionError, ValueError) as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                result["asset_errors"].append(
                    f"{field}/{object_id}: HTTP {status}"
                    if status
                    else f"{field}/{object_id}: {type(exc).__name__}"
                )
    return result


def _issue_updated_ts(issue: Mapping[str, Any]) -> datetime | None:
    """Return an aware UTC update instant, or unknown for invalid recency."""
    fields = issue.get("fields")
    value = fields.get("updated") if isinstance(fields, Mapping) else None
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value)
        if stamp.tzinfo is None:
            return None
        return stamp.astimezone(UTC)
    except (ValueError, OverflowError):
        return None


def dedupe_issues(issues, key_field="key"):
    """Return issues with a unique key, preferring the most recent push.

    When the same key appears more than once, the record with the newest
    ``fields.updated`` timestamp wins; ties fall back to the record that
    appears later in the input (i.e. the more recently pushed one, when the
    caller passes ``previous + new``). Records without a key are dropped.
    """
    best = {}
    for issue in issues:
        key = issue.get(key_field)
        if key is None:
            continue
        current = best.get(key)
        incoming_time = _issue_updated_ts(issue)
        current_time = _issue_updated_ts(current) if current is not None else None
        if current is None or (
            current_time is None
            or (incoming_time is not None and incoming_time >= current_time)
        ):
            best[key] = issue
    return list(best.values())


class _SaveDefault(Enum):
    VALUE = 0


def fetch_jira_issues(
    start_dt: datetime | None = None,
    end_dt: datetime | None = None,
    max_issues: int = 1000,
    project: str = "SDIPR",
    save_path: str | Path | None | _SaveDefault = _SaveDefault.VALUE,
    progress_cb: Callable[[int], None] | None = None,
    partial_cb: Callable[[list[dict[str, Any]]], None] | None = None,
    partial_every: int = 100,
    category_resolver: Any = None,
    *,
    invalid_row_cb: Callable[[InvalidIssue], None] | None = None,
    settings: Settings | None = None,
) -> list[dict[str, Any]]:
    """Fetch a complete desk window, reporting invalid dates without raw values."""
    config = settings or load_settings()
    if isinstance(save_path, _SaveDefault):
        save_path = config.paths.raw_snapshot
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", project):
        raise ValueError("Invalid Jira project key")
    start_dt = effective_start(project, start_dt)
    end_dt = as_utc(end_dt) if end_dt else None
    if start_dt and end_dt and start_dt > end_dt:
        return []
    if max_issues < 1:
        raise ValueError("max_issues must be positive")
    connection = _require_credentials(config)
    jira = JIRA(
        server=connection.url, basic_auth=(connection.username, connection.password)
    )
    # Confirm authentication and project visibility before accepting an empty search.
    account = jira.myself()
    jira.project(project)
    account_tz = ZoneInfo(account.get("timeZone") or desks.BERLIN.key)
    clauses = [f"project = {project}"]
    if start_dt:
        clauses.append(f"created >= '{start_dt.astimezone(account_tz):%Y-%m-%d %H:%M}'")
    if end_dt:
        # Search a superset to avoid dropping the last minute; exact filter below.
        upper = end_dt.astimezone(account_tz) + timedelta(minutes=1)
        clauses.append(f"created < '{upper:%Y-%m-%d %H:%M}'")
    jql = " AND ".join(clauses) + " ORDER BY created DESC"
    if category_resolver is None:
        from category_sync import CategoryResolver

        category_resolver = CategoryResolver(
            fetch=partial(fetch_asset_object, settings=config), settings=config
        )
    all_issues = []
    next_token = None
    seen_tokens = set()
    fetched = 0
    # Issues fetched but not yet handed to partial_cb. The callback receives
    # only the new ones so it can append rather than rewrite - rewriting the
    # whole set every flush would be quadratic on a multi-hour backfill.
    unflushed = []
    while True:
        page = jira.enhanced_search_issues(
            jql_str=jql,
            maxResults=min(100, max_issues - fetched),
            nextPageToken=next_token,
            json_result=True,
            expand=f"*all,{desks.FIELDS.central},{desks.FIELDS.branch}",
        )
        if page.get("errorMessages") or "issues" not in page:
            raise RuntimeError(f"Jira returned an invalid result for {project}")
        batch = page["issues"]
        fetched += len(batch)
        kept = [
            enrich_issue_assets(
                trim_issue(issue), category_resolver=category_resolver, settings=config
            )
            for issue in filter_issue_window(
                project, batch, start_dt, end_dt, invalid_row_cb=invalid_row_cb
            )
        ]
        all_issues.extend(kept)
        unflushed.extend(kept)
        if progress_cb:
            progress_cb(len(all_issues))
        if partial_cb and len(unflushed) >= partial_every:
            partial_cb(unflushed)
            unflushed = []
        next_token = page.get("nextPageToken")
        if not next_token:
            break
        if fetched >= max_issues:
            raise RuntimeError(
                f"{project}: issue limit reached before all pages were fetched"
            )
        if next_token in seen_tokens:
            raise RuntimeError(f"{project}: repeated pagination token")
        seen_tokens.add(next_token)
    if partial_cb and unflushed:
        partial_cb(unflushed)
    all_issues = dedupe_issues(all_issues)
    if save_path:
        Path(save_path).parent.mkdir(parents=True, exist_ok=True)
        with open(save_path, "w") as f:
            json.dump(all_issues, f)
    return all_issues


def parse_clone_links(issue):
    """
    Extract clone relationships from a JIRA issue.
    Returns a dictionary with lists: 'clones' and 'cloned_by'.
    """

    result = {
        "clones": [],  # issues this issue clones
        "cloned_by": [],  # issues that clone this one
    }

    links = issue.get("fields", {}).get("issuelinks", [])
    if not links:
        return result

    for link in links:
        # A link carries an outwardIssue, an inwardIssue, or neither - probe
        # with .get() instead of swallowing KeyError with a bare except/pass.
        outward = link.get("outwardIssue") or {}
        if "key" in outward:
            result["clones"].append(outward["key"])

        inward = link.get("inwardIssue") or {}
        if "key" in inward:
            result["cloned_by"].append(inward["key"])

    return result
