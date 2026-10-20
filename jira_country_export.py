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
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from functools import partial
from typing import Any

import pandas as pd

import asset_country as country_policy
import service_desks as desks
from country_cache import AssetIdentity
from http_policy import COUNTRY
from jira_loader import fetch_asset_object, fetch_jira_issues
from reporting_policy import BERLIN
from settings import DEFAULT_CLOUD_ID, DEFAULT_WORKSPACE_ID, Settings, load_settings

CLOUD_ID = DEFAULT_CLOUD_ID
WORKSPACE_ID = DEFAULT_WORKSPACE_ID

# Issue custom fields holding asset references, in escalation order.
ANSPRECHPARTNER_FIELD = desks.FIELDS.contact
FILIALE_FIELD = desks.FIELDS.branch
ZENTRALE_FIELD = desks.FIELDS.central
ESCALATION_FIELDS = [ANSPRECHPARTNER_FIELD, FILIALE_FIELD, ZENTRALE_FIELD]

# Compatibility exports derive from the shared policy.
COUNTRY_ATTRIBUTES = country_policy.COUNTRY_ATTRIBUTES
DEFAULT_INPUT = "data/jira_issues.json"
DEFAULT_OUTPUT = "data/issue_country.xlsx"
MAX_RETRIES = COUNTRY.attempts
BACKOFF_BASE = COUNTRY.fallback_delays[0]


def _fetch_asset_with_retry(
    object_id: str,
    cloud_id: str | None = None,
    workspace_id: str | None = None,
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    config = settings or load_settings()
    identity = AssetIdentity(
        cloud_id or config.tenant.cloud_id,
        workspace_id or config.tenant.workspace_id,
        str(object_id),
    )

    def fetch(value: AssetIdentity) -> dict[str, Any]:
        return fetch_asset_object(
            value.cloud_id, value.workspace_id, value.object_id, settings=config
        )

    return dict(
        country_policy.fetch_country_asset(identity, fetch=fetch, settings=config)
    )


def resolve_asset_country_strict(
    object_id: str,
    cloud_id: str | None = None,
    workspace_id: str | None = None,
    *,
    settings: Settings | None = None,
) -> str | None:
    """Compatibility adapter retaining failures instead of returning false no-country."""
    config = settings or load_settings()
    identity = AssetIdentity(
        cloud_id or config.tenant.cloud_id,
        workspace_id or config.tenant.workspace_id,
        str(object_id),
    )
    lookup = _country_lookup(identity, config)
    if lookup.outcome == "failed":
        raise country_policy.CountryResolutionError(lookup.error_code or "unexpected")
    return lookup.country


def resolve_asset_country(
    object_id: str,
    cloud_id: str | None = None,
    workspace_id: str | None = None,
    *,
    settings: Settings | None = None,
) -> str | None:
    """Compatibility optional-country result; failures are never memoized."""
    try:
        return resolve_asset_country_strict(
            object_id, cloud_id, workspace_id, settings=settings
        )
    except country_policy.CountryResolutionError:
        return None


def _country_lookup(
    identity: AssetIdentity, config: Settings
) -> country_policy.CountryLookup:
    return country_policy.resolve_country(
        identity,
        fetch=lambda value: _fetch_asset_with_retry(
            value.object_id, value.cloud_id, value.workspace_id, settings=config
        ),
    )


def _object_id_for_field(
    issue_fields: dict[str, Any], field: str, *, settings: Settings | None = None
) -> str | None:
    identity = country_policy.parse_asset_reference(
        issue_fields.get(field), tenant=(settings or load_settings()).tenant
    )
    return identity.object_id if identity else None


def country_for_issue(
    issue: dict[str, Any],
    *,
    settings: Settings | None = None,
    outcomes: dict[AssetIdentity, country_policy.CountryLookup] | None = None,
) -> str | None:
    """Resolve via the shared first-reference escalation and outcome policy."""
    config = settings or load_settings()
    references = country_policy.country_references(issue, tenant=config.tenant)
    if outcomes is None:
        outcomes = {
            identity: _country_lookup(identity, config)
            for _, identity in references
            if identity is not None
        }
    selection = country_policy.select_country(references, outcomes)
    return (
        selection.country
        if selection.country
        not in (
            country_policy.NOT_RESOLVED,
            country_policy.NO_COUNTRY_ON_ASSET,
            country_policy.NO_ASSETS_LINKED,
        )
        else None
    )


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
) -> dict[AssetIdentity, country_policy.CountryLookup]:
    """Warm the resolver cache by fetching every unique asset concurrently.

    The full ticket set references only a couple of thousand distinct assets,
    so resolving them in parallel turns a ~15 minute serial run into ~1 minute.
    """
    config = settings or load_settings()
    identities = sorted(
        {
            identity
            for issue in issues
            for _, identity in country_policy.country_references(
                issue, tenant=config.tenant
            )
            if identity is not None
        },
        key=lambda identity: (
            identity.cloud_id,
            identity.workspace_id,
            identity.object_id,
        ),
    )
    with ThreadPoolExecutor(max_workers=workers) as pool:
        lookups = pool.map(partial(_country_lookup, config=config), identities)
        return dict(zip(identities, lookups, strict=True))


def resolve_countries(
    issues: list[dict[str, Any]], *, settings: Settings | None = None
) -> dict[str | None, str | None]:
    """Return {issue key: country} for every issue, via the escalation logic.

    Warms the asset cache concurrently first, so this is the single entry
    point both the Excel export and the overview table can share.
    """
    config = settings or load_settings()
    outcomes = prefetch_asset_countries(issues, settings=config)
    return {
        issue.get("key"): country_for_issue(issue, settings=config, outcomes=outcomes)
        for issue in issues
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
