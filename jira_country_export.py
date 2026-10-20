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
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, date, datetime
from functools import partial
from pathlib import Path
from typing import Any
from uuid import uuid4

import pandas as pd

import asset_country as country_policy
import service_desks as desks
from country_cache import AssetIdentity
from file_io import AtomicWriteError, atomic_write
from http_policy import COUNTRY, validate_search_page
from jira_loader import fetch_asset_object, fetch_jira_issues
from operation_errors import DIAGNOSTIC_CODES, OperationDiagnostic, safe_diagnostic
from reporting_policy import BERLIN
from service_desks import berlin_day_end, berlin_day_start
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

    return dict(country_policy.fetch_country_asset(identity, fetch=fetch, settings=config))


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
        return resolve_asset_country_strict(object_id, cloud_id, workspace_id, settings=settings)
    except country_policy.CountryResolutionError:
        return None


def _country_lookup(identity: AssetIdentity, config: Settings) -> country_policy.CountryLookup:
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
    end: str | None,
    max_issues: int,
    *,
    settings: Settings | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> list[dict[str, Any]]:
    """Load issues from the saved JSON, or re-pull them from Jira."""
    if refetch:
        requested_at = now()
        if requested_at.tzinfo is None or requested_at.utcoffset() is None:
            raise ValueError("invalid_clock")
        today = requested_at.astimezone(BERLIN).date()
        start_dt = berlin_day_start(date.fromisoformat(start))
        end_day = date.fromisoformat(end) if end else today
        end_dt = min(berlin_day_end(end_day), requested_at)
        if start_dt > end_dt or project not in desks.BY_KEY or max_issues <= 0:
            raise ValueError("invalid_export_selection")
        print(f"Re-fetching issues from Jira: project={project} {start}..{end} (max {max_issues})")
        return fetch_jira_issues(
            start_dt,
            end_dt,
            max_issues=max_issues,
            project=project,
            save_path=None,
            settings=settings,
        )
    if not os.path.exists(input_path):
        raise FileNotFoundError(f"{input_path} not found. Run with --refetch to pull from Jira.")
    print(f"Loading issues from {input_path}")
    with open(input_path) as f:
        return validate_search_page({"issues": json.load(f)})["issues"]


def prefetch_asset_countries(
    issues: Sequence[Mapping[str, Any]],
    workers: int = 4,
    *,
    settings: Settings | None = None,
) -> dict[AssetIdentity, country_policy.CountryLookup]:
    """Fetch each full asset identity once within this export invocation."""
    config = settings or load_settings()
    identities = sorted(
        {
            identity
            for issue in issues
            for _, identity in country_policy.country_references(issue, tenant=config.tenant)
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


@dataclass(frozen=True)
class CountryExportResult:
    """Two-column country data accompanied by payload-free incomplete causes."""

    countries: dict[str, str | None]
    diagnostics: tuple[OperationDiagnostic, ...]


def resolve_country_report(
    issues: Sequence[Mapping[str, Any]],
    *,
    settings: Settings | None = None,
) -> CountryExportResult:
    """Resolve each country and report every failed or unverified reference.

    Args:
        issues: Saved or fetched ticket records with keyed reference fields.
        settings: One captured operation snapshot.

    Returns:
        Country mapping plus safe causes; verified empty attributes are complete.
    """
    config = settings or load_settings()
    records = validate_search_page({"issues": [dict(issue) for issue in issues]})["issues"]
    outcomes = prefetch_asset_countries(records, settings=config)
    countries: dict[str, str | None] = {}
    diagnostics: list[OperationDiagnostic] = []
    operation_id = uuid4().hex
    for issue in records:
        key = issue["key"]
        countries[key] = country_for_issue(issue, settings=config, outcomes=outcomes)
        seen: set[tuple[AssetIdentity | None, str]] = set()
        for _, identity in country_policy.country_references(issue, tenant=config.tenant):
            lookup = outcomes.get(identity) if identity is not None else None
            if lookup is not None and lookup.outcome in ("found", "no_country"):
                continue
            code = lookup.error_code if lookup is not None else "invalid_identity"
            if code is None or code not in DIAGNOSTIC_CODES:
                code = "invalid_payload"
            marker = (identity, code)
            if marker in seen:
                continue
            seen.add(marker)
            diagnostics.append(
                OperationDiagnostic(
                    operation_id,
                    "assets",
                    code,
                    project=key.split("-")[0],
                    key=key,
                    retryable=code in {"timeout", "connection", "rate_limited", "deferred"},
                )
            )
    return CountryExportResult(countries, tuple(diagnostics))


def resolve_countries(
    issues: list[dict[str, Any]],
    *,
    settings: Settings | None = None,
) -> dict[str, str | None]:
    """Preserve the mapping adapter while the report API exposes incompleteness."""
    return resolve_country_report(issues, settings=settings).countries


def build_country_frame(
    issues: list[dict[str, Any]], *, settings: Settings | None = None
) -> pd.DataFrame:
    """Build the two-column DataFrame: Jira Issue ID | Country."""
    countries = resolve_countries(issues, settings=settings)
    return pd.DataFrame(list(countries.items()), columns=["Jira Issue ID", "Country"])


def main(argv: Sequence[str] | None = None) -> int:
    """Export a workbook; return 0 complete, 1 incomplete, 2 invalid, 3 publish failure."""
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
    parser.add_argument("--start", default="2025-01-01", help="Start date YYYY-MM-DD for --refetch")
    parser.add_argument(
        "--end",
        default=None,
        help="Inclusive Berlin end day (default: today, capped at now)",
    )
    parser.add_argument(
        "--max-issues",
        type=int,
        default=100000,
        help="Max issues to fetch when --refetch",
    )
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    try:
        config = load_settings()
        paths = config.paths
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
        result = resolve_country_report(issues, settings=config)
    except (ValueError, TypeError, FileNotFoundError):
        print("Invalid country export input/configuration.")
        return 2
    except Exception as exc:  # noqa: BLE001 - safe operation boundary
        cause = safe_diagnostic(exc, operation_id=uuid4().hex, stage="fetch")
        print(f"Country export incomplete ({cause.code}).")
        return 1
    frame = pd.DataFrame(list(result.countries.items()), columns=["Jira Issue ID", "Country"])
    counts = Counter(cause.code for cause in result.diagnostics)
    print(
        f"Countries: {int(frame['Country'].notna().sum())}/{len(frame)}; "
        f"incomplete lookups: {len(result.diagnostics)}."
    )
    if counts:
        print(json.dumps(dict(sorted(counts.items()))))
    try:
        atomic_write(
            Path(args.output),
            lambda handle: frame.to_excel(handle, index=False, sheet_name="Issue Country"),
        )
    except (OSError, AtomicWriteError):
        print("Country workbook publication failed (save_failed).")
        return 3
    print(f"Wrote {len(frame)} rows.")
    return 1 if result.diagnostics else 0


if __name__ == "__main__":
    raise SystemExit(main())
