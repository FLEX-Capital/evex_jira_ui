"""Resolve a ticket's country from its linked Jira Assets objects.

Escalation order is Ansprechpartner -> Filiale -> Zentrale; the first asset
carrying a "Land" attribute wins. Lookups go through an on-disk cache, so the
Assets API is never touched during a Streamlit rerun - only the backfill
script resolves ids.
"""

import json
import math
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

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


def load_cache(path=CACHE_PATH):
    """Return {object_id: country_or_None}; {} when the file is absent."""
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def save_cache(cache, path=CACHE_PATH, *, baseline=None):
    """Merge a job's changes under a shared lock, then atomically replace.

    A baseline lets retries remove old nulls without erasing another job's
    successful resolution. Unchanged baseline entries are never written back.
    """
    from data_loading import cache_lock

    with cache_lock(path):
        current = load_cache(path)
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
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(cache, handle, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


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
        # Lazy: jira_country_export pulls in jira_loader, which needs
        # credentials, so importing it is deferred until an id actually needs
        # fetching.
        from jira_country_export import resolve_asset_country_strict

        resolver = resolve_asset_country_strict

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
    cache_path=CACHE_PATH,
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
    out["Land"] = [country for country, _ in pairs]
    out["land_quelle"] = [source for _, source in pairs]
    return out
