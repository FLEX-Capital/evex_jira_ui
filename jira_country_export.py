"""Export a two-column Excel sheet: Jira Issue ID -> Country.

The country is resolved from the issue's Ansprechpartner asset
(customfield_10689), escalating to the Filiale asset (customfield_10674)
and finally to the Zentrale asset (customfield_10673). For each field we
fetch the referenced Jira Assets object and read its "Land" attribute; the
first non-empty value wins.

Notes discovered from the live data:
  * Ansprechpartner assets do not carry a "Land" attribute, so in practice
    the country almost always comes from the Filiale or Zentrale asset --
    which is exactly why the escalation is needed.
  * Asset objects resolve only against the normal Assets workspace.
    Inaccessible objects remain unresolved; sandbox data is never substituted.

Usage:
  # Default: use the already-pulled tickets in data/jira_issues.json
  uv run --env-file .env python jira_country_export.py

  # Re-pull all tickets from Jira first (slower), then export
  uv run --env-file .env python jira_country_export.py --refetch \
      --project SDAX --start 2020-01-01 --end 2026-12-31
"""

import argparse
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from functools import cache, partial
from typing import Any

import pandas as pd
import requests

import service_desks as desks
from jira_loader import (
    fetch_asset_object,
    fetch_jira_issues,
    get_asset_attribute,
)
from reporting_policy import BERLIN
from settings import DEFAULT_CLOUD_ID, DEFAULT_WORKSPACE_ID, Settings, load_settings

CLOUD_ID = DEFAULT_CLOUD_ID
WORKSPACE_ID = DEFAULT_WORKSPACE_ID

# Issue custom fields holding asset references, in escalation order.
ANSPRECHPARTNER_FIELD = desks.FIELDS.contact
FILIALE_FIELD = desks.FIELDS.branch
ZENTRALE_FIELD = desks.FIELDS.central
ESCALATION_FIELDS = [ANSPRECHPARTNER_FIELD, FILIALE_FIELD, ZENTRALE_FIELD]

# Attribute names on an asset that hold the country.
COUNTRY_ATTRIBUTES = ["Land", "Country"]

DEFAULT_INPUT = "data/jira_issues.json"
DEFAULT_OUTPUT = "data/issue_country.xlsx"

# Retry policy for the Assets API. It rate-limits aggressively (HTTP 429),
# so transient throttling is retried with exponential backoff; genuine
# 404/403 (deleted/forbidden objects) are permanent and not retried.
MAX_RETRIES = 6
BACKOFF_BASE = 2.0  # seconds: 2, 4, 8, 16, 32, 64


def _fetch_asset_with_retry(
    object_id: str,
    cloud_id: str | None = None,
    workspace_id: str | None = None,
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Fetch an asset, retrying transient 429/5xx responses with backoff."""
    config = settings or load_settings()
    cloud_id = config.tenant.cloud_id if cloud_id is None else cloud_id
    workspace_id = config.tenant.workspace_id if workspace_id is None else workspace_id
    for attempt in range(MAX_RETRIES):
        try:
            asset = fetch_asset_object(
                cloud_id, workspace_id, object_id, settings=config
            )
            if str(asset.get("id")) != str(object_id):
                raise ValueError("Assets returned a different object identity")
            return asset
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status in (429, 500, 502, 503, 504) and attempt < MAX_RETRIES - 1:
                retry_after = (
                    exc.response.headers.get("Retry-After") if exc.response else None
                )
                delay = (
                    float(retry_after) if retry_after else BACKOFF_BASE * (2**attempt)
                )
                time.sleep(delay)
                continue
            raise  # permanent (404/403/...) or retries exhausted
    raise RuntimeError("Assets retry policy exhausted")


def resolve_asset_country(
    object_id: str,
    cloud_id: str | None = None,
    workspace_id: str | None = None,
    *,
    settings: Settings | None = None,
) -> str | None:
    """Resolve an optional country under the full current tenant identity."""
    config = settings or load_settings()
    return _resolve_asset_country_cached(
        object_id,
        config.tenant.cloud_id if cloud_id is None else cloud_id,
        config.tenant.workspace_id if workspace_id is None else workspace_id,
        config,
    )


@cache
def _resolve_asset_country_cached(
    object_id: str, cloud_id: str, workspace_id: str, settings: Settings
) -> str | None:
    """Return the country ("Land"/"Country") of a single asset, or None.

    Cached by full tenant and settings so shared assets are fetched once.
    """
    try:
        asset = _fetch_asset_with_retry(
            object_id, cloud_id, workspace_id, settings=settings
        )
    except (
        Exception
    ) as exc:  # noqa: BLE001 - report optional/isolated failures  # permanent error -> country unknown
        print(f"  ! failed to fetch asset {object_id}: {exc}")
        return None
    for attr in COUNTRY_ATTRIBUTES:
        value = get_asset_attribute(asset, attr)
        if value:
            return value
    return None


def resolve_asset_country_strict(
    object_id: str,
    cloud_id: str | None = None,
    workspace_id: str | None = None,
    *,
    settings: Settings | None = None,
) -> str | None:
    """Like resolve_asset_country, but a failed fetch raises instead of None.

    resolve_asset_country collapses "the fetch failed" and "the asset carries
    no Land" into the same None. Callers that cache the answer -
    asset_country.resolve_missing() - would otherwise persist a transient
    failure as a real result and never retry that id, so they need the two
    cases kept apart.
    """
    asset = _fetch_asset_with_retry(
        object_id, cloud_id, workspace_id, settings=settings
    )
    for attr in COUNTRY_ATTRIBUTES:
        value = get_asset_attribute(asset, attr)
        if value:
            return value
    return None


def _object_id_for_field(
    issue_fields: dict[str, Any], field: str, *, settings: Settings | None = None
) -> str | None:
    """Return the (last) asset objectId referenced by a custom field, or None."""
    refs = issue_fields.get(field) or []
    if not refs:
        return None
    ref = refs[-1]
    if ref.get("workspaceId") != (settings or load_settings()).tenant.workspace_id:
        print(f"Unresolved asset: unexpected workspace in {field}; no sandbox fallback")
        return None
    return ref.get("objectId")


def country_for_issue(
    issue: dict[str, Any], *, settings: Settings | None = None
) -> str | None:
    """Resolve an issue's country via Ansprechpartner -> Filiale -> Zentrale."""
    config = settings or load_settings()
    fields = issue.get("fields", {})
    for field in (desks.FIELDS.contact, desks.FIELDS.branch, desks.FIELDS.central):
        object_id = _object_id_for_field(fields, field, settings=config)
        if not object_id:
            continue
        country = resolve_asset_country(object_id, settings=config)
        if country:
            return country
    return None


def load_issues(
    input_path: str,
    refetch: bool,
    project: str,
    start: str,
    end: str,
    max_issues: int,
    *,
    settings: Settings | None = None,
) -> list[dict[str, Any]]:
    """Load issues from the saved JSON, or re-pull them from Jira."""
    if refetch:
        start_dt = datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=BERLIN)
        end_dt = datetime.strptime(end, "%Y-%m-%d").replace(tzinfo=BERLIN)
        print(
            f"Re-fetching issues from Jira: project={project} "
            f"{start}..{end} (max {max_issues})"
        )
        return fetch_jira_issues(
            start_dt,
            end_dt,
            max_issues=max_issues,
            project=project,
            save_path=None,
            settings=settings,
        )
    if not os.path.exists(input_path):
        raise FileNotFoundError(
            f"{input_path} not found. Run with --refetch to pull from Jira."
        )
    print(f"Loading issues from {input_path}")
    with open(input_path) as f:
        return json.load(f)


def prefetch_asset_countries(
    issues: list[dict[str, Any]], workers: int = 4, *, settings: Settings | None = None
) -> None:
    """Warm the resolver cache by fetching every unique asset concurrently.

    The full ticket set references only a couple of thousand distinct assets,
    so resolving them in parallel turns a ~15 minute serial run into ~1 minute.
    """
    config = settings or load_settings()
    unique_ids: set[str] = set()
    for issue in issues:
        fields = issue.get("fields", {})
        for field in (desks.FIELDS.contact, desks.FIELDS.branch, desks.FIELDS.central):
            oid = _object_id_for_field(fields, field, settings=config)
            if oid:
                unique_ids.add(oid)
    object_ids = sorted(unique_ids)
    print(f"Pre-fetching {len(object_ids)} unique assets with {workers} workers...")
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for _ in pool.map(partial(resolve_asset_country, settings=config), object_ids):
            done += 1
            if done % 250 == 0 or done == len(object_ids):
                print(f"  fetched {done}/{len(object_ids)} assets")


def resolve_countries(
    issues: list[dict[str, Any]], *, settings: Settings | None = None
) -> dict[str | None, str | None]:
    """Return {issue key: country} for every issue, via the escalation logic.

    Warms the asset cache concurrently first, so this is the single entry
    point both the Excel export and the overview table can share.
    """
    config = settings or load_settings()
    prefetch_asset_countries(issues, settings=config)
    return {
        issue.get("key"): country_for_issue(issue, settings=config) for issue in issues
    }


def build_country_frame(
    issues: list[dict[str, Any]], *, settings: Settings | None = None
) -> pd.DataFrame:
    """Build the two-column DataFrame: Jira Issue ID | Country."""
    countries = resolve_countries(issues, settings=settings)
    return pd.DataFrame(list(countries.items()), columns=["Jira Issue ID", "Country"])


def main():
    config = load_settings()
    paths = config.paths
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--input",
        default=None,
        help=f"Issues JSON to read (default: {DEFAULT_INPUT})",
    )
    parser.add_argument(
        "--output",
        default=None,
        help=f"Excel file to write (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--refetch",
        action="store_true",
        help="Re-pull all tickets from Jira before exporting",
    )
    parser.add_argument(
        "--project",
        default="SDAX",
        help="Project key to fetch when --refetch (default: SDAX)",
    )
    parser.add_argument(
        "--start", default="2020-01-01", help="Start date YYYY-MM-DD for --refetch"
    )
    parser.add_argument(
        "--end", default="2026-12-31", help="End date YYYY-MM-DD for --refetch"
    )
    parser.add_argument(
        "--max-issues",
        type=int,
        default=100000,
        help="Max issues to fetch when --refetch",
    )
    args = parser.parse_args()
    args.input = args.input or str(paths.raw_snapshot)
    args.output = args.output or str(paths.root / "issue_country.xlsx")

    issues = load_issues(
        args.input,
        args.refetch,
        args.project,
        args.start,
        args.end,
        args.max_issues,
        settings=config,
    )
    print(f"Resolving country for {len(issues)} issues...")
    df = build_country_frame(issues, settings=config)

    resolved = df["Country"].notna().sum()
    print(
        f"\nCountry resolved for {resolved}/{len(df)} issues "
        f"({len(df) - resolved} without a country)."
    )
    print("Country distribution:")
    print(df["Country"].value_counts(dropna=False).to_string())

    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    df.to_excel(args.output, index=False, sheet_name="Issue Country")
    print(f"\nWrote {len(df)} rows to {args.output}")


if __name__ == "__main__":
    main()
