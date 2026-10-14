"""One-off backfill: add a 'Land' column to the stored dashboard DataFrame.

The asset object ids are already in data/jira_data.pkl, so the country is
resolved without re-pulling anything from Jira. Only the asset -> Land mapping
is fetched, and only for ids the cache does not already know.

Usage:
  uv run python backfill_country.py --dry-run
  uv run python backfill_country.py
"""

import argparse
import os
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from asset_country import (
    CACHE_PATH,
    ESCALATION_COLUMNS,
    NO_ASSETS_LINKED,
    NO_COUNTRY_ON_ASSET,
    NOT_RESOLVED,
    load_cache,
    refresh_countries,
    save_cache,
)
from data_loading import (
    DATA_PATH,
    CacheChangedError,
    cache_lock,
    cache_revision,
    save_data,
)
from resolution_bands import classify_bands

DEFAULT_INPUT = "data/jira_data.pkl"

# The two inputs classify_bands needs. Both already live in the stored pickle
# for tickets processed by a prior Jira refresh; nothing here calls Jira.
HOURS_COLUMN = "time_to_resolution_biz_hours"
DONE_COLUMN = "is_done"
BAND_COLUMN = "resolution_band"


def add_resolution_band(df):
    """Return a copy of df with 'resolution_band' added, if inputs allow it.

    classify_bands only needs HOURS_COLUMN and DONE_COLUMN, both of which are
    already stored in the pickle by a prior Jira refresh - no credentials, no
    Jira call, no Assets API. When either input is missing, the Länder tab
    simply keeps showing its data-missing hint until the next refresh, so we
    warn and continue rather than crash.
    """
    missing = [c for c in (HOURS_COLUMN, DONE_COLUMN) if c not in df.columns]
    if missing:
        print(
            f"  ! column(s) absent, resolution_band NOT computed: {', '.join(missing)} "
            "- the Länder tab will keep showing its data-missing hint until the "
            "next full Jira refresh."
        )
        return df

    out = df.copy()
    # This otherwise-numeric column can hold empty strings in the stored frame:
    # older refresh code filled columns missing on one side of the upsert with ""
    # instead of NaN, and those rows are still in the cache. The current
    # upsert_jira_data() reindexes, which fills NaN, so new rows are clean.
    # Coerce either way rather than letting classify_bands compare str to float.
    hours = pd.to_numeric(out[HOURS_COLUMN], errors="coerce")
    out[BAND_COLUMN] = classify_bands(hours, out[DONE_COLUMN])
    return out


def write_pickle_atomically(df, path):
    """Write df to path so an interrupted run cannot leave a truncated pickle.

    The dashboard cache is read by data_loading.load_data() with a plain open(),
    so a half-written file is indistinguishable from a good one. data_loading's
    save_data() already writes atomically and drops the obsolete bookkeeping
    columns on the way out, so route the default cache through it verbatim rather
    than reimplementing either behaviour. A custom --output gets the same
    tempfile-then-replace treatment, minus the cache-specific column pruning.
    """
    if os.path.abspath(path) == os.path.abspath(DATA_PATH):
        save_data(df)
        return

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=target.parent, suffix=".tmp")
    os.close(handle)
    try:
        df.to_pickle(temporary)
        os.replace(temporary, target)
    except BaseException:
        if os.path.exists(temporary):
            os.remove(temporary)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default=DEFAULT_INPUT)
    parser.add_argument("--output", default=None, help="defaults to --input")
    parser.add_argument("--cache", default=CACHE_PATH)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--retry-unknown",
        action="store_true",
        help=(
            "drop cached null entries before resolving. Caches written before "
            "the strict resolver landed stored failed lookups as null, which "
            "is indistinguishable from an asset that carries no Land and is "
            "never retried; this clears them once."
        ),
    )
    args = parser.parse_args()

    output = args.output or args.input

    # Deserializing our own locally-generated pickle, not untrusted input.
    with cache_lock(output):
        revision = cache_revision(output)
        df = pd.read_pickle(args.input)  # nosec B301
    print(f"Loaded {len(df)} tickets from {args.input}")

    absent = [c for c in ESCALATION_COLUMNS if c not in df.columns]
    if absent:
        print(
            f"  ! columns absent, skipped: {', '.join(absent)} - tickets whose only "
            "asset is one of these cannot be told apart from tickets with no asset."
        )

    print(f"Cache holds {len(load_cache(args.cache))} assets")

    df, cache, stats = refresh_countries(
        df, cache_path=args.cache, retry_unknown=args.retry_unknown
    )
    if stats["dropped"]:
        print(f"  dropped {stats['dropped']} null entries; they were re-resolved")
    print(f"Newly resolved: {stats['resolved']}    failed lookups: {stats['failed']}")
    if stats["failed"]:
        print("  ! failed lookups are NOT cached; rerun to retry them")

    df = add_resolution_band(df)

    counts = df["Land"].value_counts()
    buckets = sum(
        int(counts.get(label, 0))
        for label in (NO_COUNTRY_ON_ASSET, NO_ASSETS_LINKED, NOT_RESOLVED)
    )
    print(f"  countries resolved  : {len(df) - buckets}")
    print(f"  {NO_COUNTRY_ON_ASSET} : {int(counts.get(NO_COUNTRY_ON_ASSET, 0))}")
    print(f"  {NO_ASSETS_LINKED}  : {int(counts.get(NO_ASSETS_LINKED, 0))}")
    still_unresolved = int(counts.get(NOT_RESOLVED, 0))
    if still_unresolved:
        # resolve_missing() just ran over every id in the frame, so anything
        # left here had its lookup fail in this run.
        print(f"  ! {NOT_RESOLVED} : {still_unresolved} (failed lookups; rerun)")

    if BAND_COLUMN in df.columns:
        print(f"  {BAND_COLUMN} counts:")
        for band, count in df[BAND_COLUMN].value_counts().items():
            print(f"    {band} : {int(count)}")
        print(
            "  note: bands derive from the stored time_to_resolution_biz_hours, "
            "which reflects whatever holiday calendar was in force when it was "
            "computed; a Jira refresh recomputes it with subdiv='BW' "
            "(measured difference: 12 tickets)."
        )

    if args.dry_run:
        print("Dry run - nothing written.")
        return

    with cache_lock(output):
        if cache_revision(output) != revision:
            raise CacheChangedError("Cache changed during country backfill; rerun.")
        save_cache(cache, args.cache)
        # Same path test as write_pickle_atomically(), which routes the live cache
        # through save_data(). Comparing raw strings here instead would let
        # `--output ./data/jira_data.pkl` overwrite the cache with no backup.
        if os.path.abspath(output) == os.path.abspath(args.input):
            backup = f"{args.input}.bak-{datetime.now(UTC):%Y%m%d-%H%M%S-%f}"
            shutil.copy2(args.input, backup)
            print(f"Backup written to {backup}")
        write_pickle_atomically(df, output)
    print(f"Wrote {len(df)} tickets to {output}")


if __name__ == "__main__":
    main()
