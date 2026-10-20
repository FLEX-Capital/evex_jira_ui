"""Resolve a ticket's country from its linked Jira Assets objects.

Escalation order is Ansprechpartner -> Filiale -> Zentrale; the first asset
carrying a "Land" attribute wins. Lookups go through an on-disk cache, so the
Assets API is never touched during a Streamlit rerun; only explicit maintenance
resolves ids.
"""

import math
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any, Literal

import pandas as pd
import requests

import service_desks as desks
from country_cache import (
    AssetIdentity,
    CountryCache,
    CountryEntry,
    choose_country_entry,
    is_country_fresh,
    load_country_cache,
    merge_country_entries,
)
from http_policy import COUNTRY, RetryDeferred, with_retries
from settings import (
    DEFAULT_TENANT,
    TENANT_ATTR,
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


def normalise_object_id(value: object) -> str | None:
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
    return AssetIdentity(tenant.cloud_id, tenant.workspace_id, object_id) if object_id else None


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
                "object_id": (parsed.object_id if parsed else normalise_object_id(raw_id)),
                "provenance": (
                    "modern" if parsed and modern else "legacy_unverified" if parsed else "rejected"
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
            identity = parse_asset_reference(record["object_id"], tenant=tenant, legacy=True)
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
                if attribute["objectTypeAttribute"]["name"].casefold() != name.casefold():
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
    country = NOT_RESOLVED if failures else NO_COUNTRY_ON_ASSET if references else NO_ASSETS_LINKED
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
    lookup = resolve_country(identity, fetch=partial(fetch_country_asset, settings=config))
    if lookup.outcome == "failed":
        raise CountryResolutionError(lookup.error_code or "unexpected")
    return lookup.country


def _as_country_cache(cache: CountryCache | Mapping[str, str | None]) -> CountryCache:
    return cache if isinstance(cache, CountryCache) else CountryCache({}, dict(cache))


def load_cache(path: str | Path | None = None, *, settings: Settings | None = None) -> CountryCache:
    """Load typed country state; bare legacy values remain unverified metadata."""
    config = settings or load_settings()
    return load_country_cache(config.paths.countries if path is None else Path(path))


def save_cache(
    cache: CountryCache | Mapping[str, str | None],
    path: str | Path | None = None,
    *,
    baseline: CountryCache | Mapping[str, str | None] | None = None,
    settings: Settings | None = None,
) -> CountryCache:
    """Merge verified identities and explicit legacy metadata outside ticket locks."""
    config = settings or load_settings()
    prepared = _as_country_cache(cache)
    original = _as_country_cache(baseline).legacy_unverified if baseline is not None else None
    return merge_country_entries(
        config.paths.countries if path is None else Path(path),
        list(prepared.entries.values()),
        legacy=prepared.legacy_unverified,
        baseline=original,
    )


def resolve_missing(
    object_ids: Iterable[object],
    cache: dict[str, str | None],
    resolver: Callable[[str], str | None] | None = None,
    workers: int = 4,
) -> tuple[dict[str, str | None], int, int]:
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

    def _safe(object_id: str) -> tuple[str, str | None, bool]:
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


def classify_country(
    ansprechpartner: object, filiale: object, zentrale: object, cache: Mapping[str, str | None]
) -> tuple[str, str]:
    """Return (country, source) for one ticket.

    country is a country name, NO_COUNTRY_ON_ASSET, NO_ASSETS_LINKED or
    NOT_RESOLVED; source names the field the country came from, or NO_SOURCE.
    """
    linked = False
    unresolved = False
    for source, raw in zip(ESCALATION_COLUMNS, (ansprechpartner, filiale, zentrale), strict=True):
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


def collect_object_ids(df: pd.DataFrame) -> list[object]:
    """Every asset cell from the escalation columns present in the frame."""
    values = []
    for column in ESCALATION_COLUMNS:
        if column in df.columns:
            values.extend(df[column].tolist())
    return values


def _tenant_for_frame(frame: pd.DataFrame) -> TenantIdentity | None:
    value = frame.attrs.get(TENANT_ATTR)
    if isinstance(value, dict) and all(
        isinstance(value.get(key), str) for key in ("cloud_id", "workspace_id")
    ):
        return TenantIdentity(value["cloud_id"], value["workspace_id"])
    return None


def _row_references(
    row: pd.Series, tenant: TenantIdentity | None, *, allow_legacy: bool
) -> tuple[list[tuple[str, AssetIdentity | None]], bool]:
    records = row.get("asset_references")
    references: list[tuple[str, AssetIdentity | None]] = []
    unverified = False
    if isinstance(records, list):
        for record in records:
            source = record.get("source")
            if source not in ESCALATION_COLUMNS:
                continue
            identity = None
            if record.get("provenance") == "modern":
                candidate = AssetIdentity(
                    record.get("cloud_id"),
                    record.get("workspace_id"),
                    record.get("object_id"),
                )
                if all(
                    isinstance(value, str) and value
                    for value in (
                        candidate.cloud_id,
                        candidate.workspace_id,
                        candidate.object_id,
                    )
                ) and (
                    tenant is None
                    or (candidate.cloud_id, candidate.workspace_id)
                    == (tenant.cloud_id, tenant.workspace_id)
                ):
                    identity = candidate
            elif record.get("provenance") == "legacy_unverified":
                unverified = True
                if allow_legacy and tenant is not None:
                    identity = parse_asset_reference(
                        record.get("object_id"), tenant=tenant, legacy=True
                    )
            references.append((source, identity))
    else:
        for source in ESCALATION_COLUMNS:
            object_id = normalise_object_id(row.get(source))
            if object_id is not None:
                unverified = True
                references.append(
                    (
                        source,
                        (
                            parse_asset_reference(object_id, tenant=tenant, legacy=True)
                            if allow_legacy and tenant
                            else None
                        ),
                    )
                )
    return references, unverified


def attach_country(
    df: pd.DataFrame,
    cache: CountryCache | Mapping[str, str | None],
    *,
    allow_legacy: bool = False,
    settings: Settings | None = None,
) -> pd.DataFrame:
    """Attach verified matching countries without refreshing or guessing legacy ownership.

    Stored legacy Land remains visible on ordinary reads. Only explicit
    maintenance enables legacy references after tenant-scoped revalidation.
    """
    out = df.copy()
    cache = _as_country_cache(cache)
    tenant = settings.tenant if settings else _tenant_for_frame(df)
    pairs = []
    for _, row in out.iterrows():
        references, unverified = _row_references(row, tenant, allow_legacy=allow_legacy)
        stored = row.get("Land")
        if unverified and not allow_legacy and isinstance(stored, str) and stored.strip():
            pairs.append(
                (
                    stored,
                    (
                        row.get("land_quelle")
                        if isinstance(row.get("land_quelle"), str)
                        else NO_SOURCE
                    ),
                )
            )
            continue
        outcomes = {
            identity: CountryLookup(identity, entry.outcome, entry.country, None)
            for _, identity in references
            if identity is not None and (entry := cache.entries.get(identity)) is not None
        }
        selected = select_country(references, outcomes)
        pairs.append((selected.country, selected.source))
    out["Land"] = [country for country, _ in pairs]
    out["land_quelle"] = [source for _, source in pairs]
    return out


def _reference_status(
    references: Sequence[Sequence[tuple[str, AssetIdentity | None]]],
    cache: CountryCache,
    *,
    now: datetime,
) -> dict[str, int]:
    identities = {identity for group in references for _, identity in group if identity is not None}
    return {
        "stale": sum(
            identity in cache.entries and not is_country_fresh(cache.entries[identity], now=now)
            for identity in identities
        ),
        "unverified": sum(identity not in cache.entries for identity in identities),
        "unresolved": sum(identity is None for group in references for _, identity in group),
    }


def country_cache_status(
    df: pd.DataFrame,
    cache: CountryCache | Mapping[str, str | None],
    *,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    """Count selected stale/unverified asset identities and rejected references offline."""
    config = settings or load_settings()
    references = [
        _row_references(row, config.tenant, allow_legacy=True)[0] for _, row in df.iterrows()
    ]
    return _reference_status(references, _as_country_cache(cache), now=now or datetime.now(UTC))


def refresh_countries(
    df: pd.DataFrame,
    cache_path: str | Path | None = None,
    retry_unknown: bool = False,
    resolver: Callable[[str], str | None] | None = None,
    workers: int = 4,
    *,
    cache: CountryCache | Mapping[str, str | None] | None = None,
    force: bool = False,
    now: Callable[[], datetime] | None = None,
    settings: Settings | None = None,
) -> tuple[pd.DataFrame, CountryCache, dict[str, int]]:
    """Prepare explicit refresh results; callers merge countries then patch tickets.

    Failure never replaces a verified value. Rerenders only use attach_country;
    they cannot invoke this network boundary as an incidental cache read.
    """
    config = settings or load_settings()
    marker = df.attrs.get(TENANT_ATTR)
    if marker is None and config.tenant != DEFAULT_TENANT:
        raise SettingsError("legacy_tenant_unknown", ("JIRA_CLOUD_ID", "JIRA_WORKSPACE_ID"))
    if marker is not None and marker != config.tenant.as_dict():
        raise SettingsError("tenant_mismatch", ("JIRA_CLOUD_ID", "JIRA_WORKSPACE_ID"))
    clock = now or (lambda: datetime.now(UTC))
    requested_at = clock()
    current = (
        load_cache(cache_path, settings=config) if cache is None else _as_country_cache(cache)
    ).copy()
    references = [
        _row_references(row, config.tenant, allow_legacy=True)[0] for _, row in df.iterrows()
    ]
    identities = {identity for group in references for _, identity in group if identity is not None}
    pending = sorted(
        (
            identity
            for identity in identities
            if force
            or (entry := current.entries.get(identity)) is None
            or not is_country_fresh(entry, now=requested_at)
            or (retry_unknown and entry.outcome == "no_country")
        ),
        key=lambda identity: (
            identity.cloud_id,
            identity.workspace_id,
            identity.object_id,
        ),
    )
    dropped = 0
    if retry_unknown:
        for key in list(current.legacy_unverified):
            if current.legacy_unverified[key] is None:
                current.legacy_unverified.pop(key)
                dropped += 1

    def resolve_lookup(identity: AssetIdentity) -> CountryLookup:
        if resolver is None:
            return resolve_country(identity, fetch=partial(fetch_country_asset, settings=config))
        try:
            value = resolver(identity.object_id)
            if value is not None and not isinstance(value, str):
                return CountryLookup(identity, "failed", None, "invalid_payload")
            value = value.strip() or None if value is not None else None
            return CountryLookup(identity, "found" if value else "no_country", value, None)
        except Exception as exc:
            return CountryLookup(identity, "failed", None, _country_error_code(exc))

    def lookup(identity: AssetIdentity) -> tuple[CountryLookup, datetime]:
        outcome = resolve_lookup(identity)
        # Timestamp response completion in its worker, before ordered consumption
        # can be delayed by an unrelated slower asset.
        return outcome, clock()

    failed = resolved = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for identity, (outcome, completed_at) in zip(
            pending, pool.map(lookup, pending), strict=True
        ):
            if outcome.outcome not in ("found", "no_country"):
                failed += 1
                continue
            entry = CountryEntry(
                identity,
                "found" if outcome.outcome == "found" else "no_country",
                outcome.country,
                completed_at,
            )
            current.entries[identity] = choose_country_entry(current.entries.get(identity), entry)
            resolved += 1
    stats = {
        "dropped": dropped,
        "resolved": resolved,
        "failed": failed,
        **_reference_status(references, current, now=requested_at),
    }
    return (
        attach_country(df, current, allow_legacy=True, settings=config),
        current,
        stats,
    )
