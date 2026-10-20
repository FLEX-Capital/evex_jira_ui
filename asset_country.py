"""Resolve a ticket's country from its linked Jira Assets objects.

Escalation order is Ansprechpartner -> Filiale -> Zentrale; the first asset
carrying a "Land" attribute wins. Lookups go through an on-disk cache, so the
Assets API is never touched during a Streamlit rerun - only the backfill
script resolves ids.
"""

import json
import math
import os
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import Any, Literal

import pandas as pd
import requests

import service_desks as desks
from country_cache import AssetIdentity
from file_io import write_json_atomic
from http_policy import COUNTRY, RetryDeferred, with_retries
from settings import (
    DEFAULT_TENANT,
    Settings,
    SettingsError,
    TenantIdentity,
    load_settings,
)

CACHE_PATH = "data/asset_country.json"

NO_COUNTRY_ON_ASSET = "Kein Land am Asset"
NO_ASSETS_LINKED = "Keine Assets verknüpft"
# The ticket links an asset the cache has never been asked about - typically a
# ticket added by a dashboard refresh since the last backfill run. Distinct from
# NO_COUNTRY_ON_ASSET, which means the asset was fetched and carries no Land.
NOT_RESOLVED = "Land noch nicht ermittelt"
NO_SOURCE = "—"

# Escalation order; also the DataFrame columns holding the asset ids.
ESCALATION_COLUMNS = ["ansprechpartner", "filiale", "zentrale"]

_ABSENT = {"", "nan", "none", "id_", "id_nan", "id_none", "<na>"}


def normalise_object_id(value):
    """Return the bare objectId for a stored 'ID_<objectId>' cell, or None."""
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    text = str(value).strip()
    if text.lower() in _ABSENT:
        return None
    if text.startswith("ID_"):
        text = text[len("ID_") :]
    text = text.strip()
    if not text or text.lower() in _ABSENT:
        return None
    return text


COUNTRY_ATTRIBUTES = ("Land", "Country")


@dataclass(frozen=True)
class CountryLookup:
    """A verified result or safe incomplete outcome for one reference."""

    identity: AssetIdentity | None
    outcome: Literal["found", "no_country", "unresolved", "failed"]
    country: str | None
    error_code: str | None


@dataclass(frozen=True)
class CountrySelection:
    """The first real escalation country with earlier failures retained."""

    country: str
    source: str
    failures: tuple[CountryLookup, ...]


class CountryResolutionError(ValueError):
    """A safe country failure code without remote response details."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def parse_asset_reference(
    value: object, *, tenant: TenantIdentity, legacy: bool = False
) -> AssetIdentity | None:
    """Select the first reference and validate its workspace/cloud ownership.

    Bare IDs are accepted only for explicitly legacy input; callers retain that
    unverified provenance until an explicit tenant-scoped network lookup.
    """
    if isinstance(value, list):
        if not value:
            return None
        value = value[0]
    if isinstance(value, Mapping):
        workspace = value.get("workspaceId")
        cloud = value.get("cloudId", value.get("cloud_id"))
        if workspace is None:
            if not legacy:
                return None
        elif workspace != tenant.workspace_id:
            return None
        if cloud is not None and cloud != tenant.cloud_id:
            return None
        value = value.get("objectId")
    elif not legacy:
        return None
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return None
    object_id = normalise_object_id(value)
    return (
        AssetIdentity(tenant.cloud_id, tenant.workspace_id, object_id)
        if object_id
        else None
    )


def issue_asset_references(
    issue: Mapping[str, Any], *, tenant: TenantIdentity
) -> list[dict[str, Any]]:
    """Retain first-reference provenance; absent fields have no record."""
    fields = issue.get("fields") or {}
    identity = (issue.get("assets_cloud_id"), issue.get("assets_workspace_id"))
    modern = identity == (tenant.cloud_id, tenant.workspace_id)
    legacy = identity == (None, None) and tenant == DEFAULT_TENANT
    records = []
    for field, source in zip(
        (desks.FIELDS.contact, desks.FIELDS.branch, desks.FIELDS.central),
        ESCALATION_COLUMNS,
        strict=True,
    ):
        values = fields.get(field)
        if values is None or values == [] or values == "":
            continue
        parsed = (
            parse_asset_reference(values, tenant=tenant, legacy=legacy)
            if modern or legacy
            else None
        )
        first = values[0] if isinstance(values, list) and values else values
        raw_id = first.get("objectId") if isinstance(first, Mapping) else first
        records.append(
            {
                "source": source,
                "cloud_id": parsed.cloud_id if parsed and modern else None,
                "workspace_id": parsed.workspace_id if parsed and modern else None,
                "object_id": (
                    parsed.object_id if parsed else normalise_object_id(raw_id)
                ),
                "provenance": (
                    "modern"
                    if parsed and modern
                    else "legacy_unverified" if parsed else "rejected"
                ),
            }
        )
    return records


def country_references(
    issue: Mapping[str, Any], *, tenant: TenantIdentity
) -> tuple[tuple[str, AssetIdentity | None], ...]:
    """Prepare checked references for explicit lookup; rejected identities stay None."""
    result = []
    for record in issue_asset_references(issue, tenant=tenant):
        identity = None
        if record["provenance"] == "modern":
            identity = AssetIdentity(
                record["cloud_id"], record["workspace_id"], record["object_id"]
            )
        elif record["provenance"] == "legacy_unverified":
            identity = parse_asset_reference(
                record["object_id"], tenant=tenant, legacy=True
            )
        result.append((record["source"], identity))
    return tuple(result)


def _validated_asset(identity: AssetIdentity, asset: object) -> Mapping[str, Any]:
    if not isinstance(asset, Mapping):
        raise CountryResolutionError("invalid_payload")
    if str(asset.get("id")) != identity.object_id:
        raise CountryResolutionError("invalid_identity")
    for key, expected in (
        ("workspaceId", identity.workspace_id),
        ("cloudId", identity.cloud_id),
    ):
        if key in asset and asset[key] != expected:
            raise CountryResolutionError("invalid_identity")
    return asset


def fetch_country_asset(
    identity: AssetIdentity,
    *,
    fetch: Callable[[AssetIdentity], Mapping[str, Any]] | None = None,
    settings: Settings | None = None,
    now: Callable[[], datetime] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> Mapping[str, Any]:
    """Read an asset with the country retry budget and permanent identity checks."""
    config = settings or load_settings()
    if fetch is None:
        from jira_loader import fetch_asset_object_uncached

        def fetch_default(value: AssetIdentity) -> Mapping[str, Any]:
            return fetch_asset_object_uncached(
                value.cloud_id, value.workspace_id, value.object_id, settings=config
            )

        fetch = fetch_default
    return with_retries(
        lambda: _validated_asset(identity, fetch(identity)),
        policy=COUNTRY,
        now=now or (lambda: datetime.now(UTC)),
        sleep=sleep or time.sleep,
    )


def _country_error_code(exc: Exception) -> str:
    if isinstance(exc, CountryResolutionError):
        return exc.code
    if isinstance(exc, RetryDeferred):
        return "deferred"
    if isinstance(exc, requests.Timeout):
        return "timeout"
    if isinstance(exc, requests.ConnectionError):
        return "connection"
    response = getattr(exc, "response", None)
    status = response.status_code if response is not None else None
    if isinstance(exc, PermissionError) or status in (401, 403):
        return "permission"
    return (
        {404: "not_found", 429: "rate_limited"}.get(status, "unexpected")
        if isinstance(status, int)
        else "unexpected"
    )


def resolve_country(
    identity: AssetIdentity, *, fetch: Callable[[AssetIdentity], Mapping[str, Any]]
) -> CountryLookup:
    """Distinguish verified country/empty attributes from a failed complete read."""
    try:
        asset = _validated_asset(identity, fetch(identity))
        attributes = asset.get("attributes")
        if not isinstance(attributes, list) or any(
            not isinstance(attribute, Mapping)
            or not isinstance(attribute.get("objectTypeAttribute"), Mapping)
            or not isinstance(attribute["objectTypeAttribute"].get("name"), str)
            or not isinstance(attribute.get("objectAttributeValues"), list)
            for attribute in attributes
        ):
            raise CountryResolutionError("invalid_payload")
        for name in COUNTRY_ATTRIBUTES:
            for attribute in attributes:
                if (
                    attribute["objectTypeAttribute"]["name"].casefold()
                    != name.casefold()
                ):
                    continue
                for value in attribute["objectAttributeValues"]:
                    if not isinstance(value, Mapping):
                        raise CountryResolutionError("invalid_payload")
                    text = value.get("displayValue") or value.get("value")
                    if text is None or text == "":
                        continue
                    if not isinstance(text, str):
                        raise CountryResolutionError("invalid_payload")
                    if text.strip():
                        return CountryLookup(identity, "found", text.strip(), None)
        return CountryLookup(identity, "no_country", None, None)
    except Exception as exc:
        return CountryLookup(identity, "failed", None, _country_error_code(exc))


def select_country(
    references: Sequence[tuple[str, AssetIdentity | None]],
    outcomes: Mapping[AssetIdentity, CountryLookup],
) -> CountrySelection:
    """Choose the first real country while preserving earlier unresolved/failures."""
    failures: list[CountryLookup] = []
    for source, identity in references:
        lookup = outcomes.get(identity) if identity is not None else None
        if lookup is None:
            lookup = CountryLookup(
                identity,
                "unresolved",
                None,
                "invalid_identity" if identity is None else "unresolved",
            )
        if lookup.outcome == "found" and lookup.country:
            return CountrySelection(lookup.country, source, tuple(failures))
        if lookup.outcome != "no_country":
            failures.append(lookup)
    country = (
        NOT_RESOLVED
        if failures
        else NO_COUNTRY_ON_ASSET if references else NO_ASSETS_LINKED
    )
    return CountrySelection(country, NO_SOURCE, tuple(failures))


def resolve_asset_country_strict(
    object_id: str,
    cloud_id: str | None = None,
    workspace_id: str | None = None,
    *,
    settings: Settings | None = None,
) -> str | None:
    """Compatibility adapter: a verified empty country returns None; failure raises."""
    config = settings or load_settings()
    identity = AssetIdentity(
        cloud_id or config.tenant.cloud_id,
        workspace_id or config.tenant.workspace_id,
        str(object_id),
    )
    lookup = resolve_country(
        identity, fetch=partial(fetch_country_asset, settings=config)
    )
    if lookup.outcome == "failed":
        raise CountryResolutionError(lookup.error_code or "unexpected")
    return lookup.country


def load_cache(path=None, *, settings=None):
    """Return {object_id: country_or_None}; {} when the file is absent."""
    config = settings or load_settings()
    if config.tenant != DEFAULT_TENANT:
        return {}
    path = config.paths.countries if path is None else path
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def save_cache(cache, path=None, *, baseline=None, settings=None):
    """Merge a job's changes under a shared lock, then atomically replace.

    A baseline lets retries remove old nulls without erasing another job's
    successful resolution. Unchanged baseline entries are never written back.
    """
    from data_loading import cache_lock

    config = settings or load_settings()
    if config.tenant != DEFAULT_TENANT:
        raise SettingsError(
            "country_tenant_schema_required", ("JIRA_CLOUD_ID", "JIRA_WORKSPACE_ID")
        )
    path = config.paths.countries if path is None else path
    with cache_lock(path):
        current = load_cache(path, settings=config)
        if baseline is None:
            current.update(cache)
        else:
            missing = object()
            for key in baseline.keys() | cache.keys():
                before = baseline.get(key, missing)
                after = cache.get(key, missing)
                if before == after or current.get(key, missing) != before:
                    continue
                if after is missing:
                    current.pop(key, None)
                else:
                    current[key] = after
        _write_cache(current, path)
        return current


def _write_cache(cache, path):
    write_json_atomic(path, cache)


def resolve_missing(object_ids, cache, resolver=None, workers=4):
    """Resolve ids absent from the cache. Returns (cache, resolved, failed).

    A failed lookup (deleted or forbidden asset, API outage) is counted but
    NOT written to the cache - caching it as None would be indistinguishable
    from an asset that genuinely carries no Land, and `object_id not in cache`
    would then skip that id on every later run. This only holds because the
    default resolver raises on failure; resolve_asset_country (which returns
    None instead) must not be used here.
    """
    pending = sorted(
        {
            object_id
            for object_id in (normalise_object_id(v) for v in object_ids)
            if object_id is not None and object_id not in cache
        }
    )
    if not pending:
        return cache, 0, 0

    if resolver is None:
        # Capture one operation snapshot before concurrent network reads.
        resolver = partial(resolve_asset_country_strict, settings=load_settings())

    def _safe(object_id):
        try:
            return object_id, resolver(object_id), False
        except Exception:
            return object_id, None, True

    failed = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for object_id, country, errored in pool.map(_safe, pending):
            if errored:
                failed += 1
                continue
            cache[object_id] = country
    return cache, len(pending) - failed, failed


def classify_country(ansprechpartner, filiale, zentrale, cache):
    """Return (country, source) for one ticket.

    country is a country name, NO_COUNTRY_ON_ASSET, NO_ASSETS_LINKED or
    NOT_RESOLVED; source names the field the country came from, or NO_SOURCE.
    """
    linked = False
    unresolved = False
    for source, raw in zip(
        ESCALATION_COLUMNS, (ansprechpartner, filiale, zentrale), strict=True
    ):
        object_id = normalise_object_id(raw)
        if object_id is None:
            continue
        linked = True
        if object_id not in cache:
            # Never looked up, so we cannot claim the asset has no Land.
            unresolved = True
            continue
        country = cache[object_id]
        if country:
            return country, source
    if not linked:
        return NO_ASSETS_LINKED, NO_SOURCE
    if unresolved:
        return NOT_RESOLVED, NO_SOURCE
    return NO_COUNTRY_ON_ASSET, NO_SOURCE


def collect_object_ids(df):
    """Every asset cell from the escalation columns present in the frame."""
    values = []
    for column in ESCALATION_COLUMNS:
        if column in df.columns:
            values.extend(df[column].tolist())
    return values


def refresh_countries(
    df,
    cache_path=None,
    retry_unknown=False,
    resolver=None,
    workers=4,
    *,
    cache=None,
):
    """Resolve unknown asset ids, then rewrite Land/land_quelle on every row.

    Returns (df, cache, stats). The cache is NOT written - the caller decides,
    so backfill_country's --dry-run can inspect the outcome without persisting
    it. Both the CLI and the dashboard's Länder button go through here, so the
    two cannot drift apart.

    Supply cache to resolve against a captured baseline; that dictionary is
    copied so it can later be passed unchanged to save_cache(baseline=...).

    retry_unknown drops entries cached as None first. Those are ambiguous in
    caches written before the strict resolver landed: a failed lookup and an
    asset that genuinely carries no Land were stored identically, and
    resolve_missing() skips any id already present.
    """
    cache = load_cache(cache_path) if cache is None else cache.copy()
    dropped = 0
    if retry_unknown:
        stale = [key for key, value in cache.items() if value is None]
        for key in stale:
            del cache[key]
        dropped = len(stale)

    cache, resolved, failed = resolve_missing(
        collect_object_ids(df), cache, resolver=resolver, workers=workers
    )
    stats = {"dropped": dropped, "resolved": resolved, "failed": failed}
    return attach_country(df, cache), cache, stats


def attach_country(df, cache):
    """Return a copy of df with 'Land' and 'land_quelle' columns added."""
    out = df.copy()
    blank = pd.Series("", index=out.index, dtype="object")
    columns = [
        out[name] if name in out.columns else blank for name in ESCALATION_COLUMNS
    ]
    pairs = [classify_country(a, f, z, cache) for a, f, z in zip(*columns, strict=True)]
    if "asset_references" in out:
        for position, references in enumerate(out["asset_references"]):
            if isinstance(references, list) and any(
                reference.get("provenance") == "rejected" for reference in references
            ):
                if pairs[position][0] in (
                    NO_ASSETS_LINKED,
                    NO_COUNTRY_ON_ASSET,
                    NOT_RESOLVED,
                ):
                    pairs[position] = (NOT_RESOLVED, NO_SOURCE)
    out["Land"] = [country for country, _ in pairs]
    out["land_quelle"] = [source for _, source in pairs]
    return out
