"""Backfill ``data/jira_data.pkl`` with a full range of SDIPR, SDAX, and SDEU issues.

Why this exists instead of the sidebar button: the Streamlit refresh runs the
fetch inline in a script run, so any browser interaction or reconnect reruns
the script and kills the fetch mid-flight. A year of issues takes long enough
that this is close to guaranteed. Run this from a terminal instead.

    uv run python backfill_jira.py                  # past year, all desks (SDEU: since 2026-09-01)
    uv run python backfill_jira.py --days 60        # shorter window
    uv run python backfill_jira.py --resume         # reuse existing checkpoints
    uv run python backfill_jira.py --project SDIPR  # one project only
    uv run python backfill_jira.py --start 2025-01-01 --end 2025-03-17  # one slice

The last form is how you extend an existing collection backwards without
refetching what you already have: the merge is an upsert, so a bounded slice is
added to the pickle and everything already there is kept.

Each project's raw issues are checkpointed to ``data/backfill_<PROJECT>.json``
as soon as its fetch finishes, so a crash during the second project does not
throw away the first one's hours of work. ``--resume`` picks those back up.

The existing pickle is copied to ``data/jira_data.pkl.bak-<timestamp>`` before
anything is written, and the merge is an upsert: existing rows are updated in
place and unseen rows appended, so nothing already collected is lost.
"""

import argparse
import json
import os
import shutil
import time
from datetime import UTC, date, datetime
from datetime import time as dt_time
from datetime import timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from data_loading import DATA_PATH, load_data, save_data
from desk_sync import write_json_atomic
from jira_loader import (
    CLOUD_ID,
    WORKSPACE_ID,
    dedupe_issues,
    enrich_issue_assets,
    fetch_jira_issues,
)
from service_desks import BY_KEY, as_utc, effective_start, filter_issue_window

CHECKPOINT_TMPL = "data/backfill_{project}.json"
SAVE_EVERY = 100


def _partial_paths(project):
    """Partial-log paths, derived from the checkpoint path rather than declared.

    Partial progress within a single fetch. The completed checkpoint is one JSON
    document; this is append-only NDJSON, so flushing every SAVE_EVERY issues
    costs the size of the flush, not of everything fetched so far.

    Deriving these from CHECKPOINT_TMPL is deliberate. As separate constants they
    kept pointing at the real data/ directory in tests that only patched
    CHECKPOINT_TMPL, so running the suite created and then deleted the partial
    log of a backfill running concurrently - destroying the exact thing this
    mechanism exists to protect. Now redirecting the checkpoint redirects these.
    """
    base = CHECKPOINT_TMPL.format(project=project)
    stem = base.removesuffix(".json")
    return f"{stem}.partial.ndjson", f"{stem}.partial.json"


def begin_partial(project, start_dt, end_dt):
    """Start a fresh partial log, returning an appender for fetch_jira_issues.

    The metadata is written first and separately: a partial log is only safe to
    resume from if we know which window produced it, and that has to be on disk
    before the first issue is, not after.
    """
    log_path, meta_path = _partial_paths(project)
    os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
    write_json_atomic(
        meta_path,
        {
            "version": 1,
            "project": project,
            "start": start_dt.isoformat(),
            "end": end_dt.isoformat(),
            "assets_cloud_id": CLOUD_ID,
            "assets_workspace_id": WORKSPACE_ID,
        },
    )
    open(log_path, "w").close()

    def append(issues):
        # Append + flush + fsync: a killed process must leave every line it
        # reported as saved actually on disk, not in a buffer.
        with open(log_path, "a") as handle:
            handle.writelines(json.dumps(issue) + "\n" for issue in issues)
            handle.flush()
            os.fsync(handle.fileno())
        print(f"  [{project}] checkpointed +{len(issues)} issues", flush=True)

    return append


def _reopen_partial(project):
    """Appender for a partial log that already exists, leaving its lines intact."""
    log_path, _ = _partial_paths(project)

    def append(issues):
        with open(log_path, "a") as handle:
            handle.writelines(json.dumps(issue) + "\n" for issue in issues)
            handle.flush()
            os.fsync(handle.fileno())
        print(f"  [{project}] checkpointed +{len(issues)} issues", flush=True)

    return append


def load_partial(project, start_dt, end_dt):
    """Return (issues, oldest_created) from a resumable partial log, else ([], None).

    A log whose window does not match the requested one is ignored rather than
    merged: silently mixing two windows is how a backfill ends up with a gap it
    cannot detect.
    """
    log_path, meta_path = _partial_paths(project)
    if not (os.path.exists(log_path) and os.path.exists(meta_path)):
        return [], None
    with open(meta_path) as handle:
        meta = json.load(handle)
    if (
        meta.get("project") != project
        or meta.get("assets_cloud_id") != CLOUD_ID
        or meta.get("assets_workspace_id") != WORKSPACE_ID
        or as_utc(datetime.fromisoformat(meta["start"])) != start_dt
    ):
        return [], None

    issues = []
    with open(log_path) as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                issues.append(json.loads(line))
            except json.JSONDecodeError:
                # Only the final line can be torn, and only if the process died
                # mid-write. Everything before it is still good.
                print(f"  [{project}] discarding torn final line", flush=True)
                break

    issues = dedupe_issues(issues)
    if not issues:
        return [], None
    oldest = min(
        as_utc(datetime.fromisoformat(issue["fields"]["created"])) for issue in issues
    )
    return issues, oldest


def clear_partial(project):
    for path in _partial_paths(project):
        if os.path.exists(path):
            os.unlink(path)


def _fmt_duration(seconds):
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s"


def _make_progress(project, started):
    """Print a one-line progress update per fetched page."""
    state = {"last": 0.0}

    def report(count):
        now = time.time()
        # Throttle to at most one line every 5s so long runs stay readable.
        if now - state["last"] < 5:
            return
        state["last"] = now
        elapsed = now - started
        rate = count / elapsed if elapsed else 0
        print(
            f"  [{project}] {count:>6} issues | {_fmt_duration(elapsed)} elapsed "
            f"| {rate:.1f} issues/s",
            flush=True,
        )

    return report


def fetch_project(project, start_dt, end_dt, resume):
    """Fetch one project's issues, using/creating a raw checkpoint file."""
    start_dt = effective_start(project, start_dt)
    end_dt = as_utc(end_dt)
    if start_dt > end_dt:
        return []
    checkpoint = CHECKPOINT_TMPL.format(project=project)
    issues = []
    fetch_start = start_dt
    if resume and os.path.exists(checkpoint):
        with open(checkpoint) as f:
            saved = json.load(f)
        # Date-only legacy checkpoints have neither exact coverage nor trustworthy
        # enrichment provenance. Refetch the effective window into normal Assets.
        if saved.get("version") == 2:
            saved_start = as_utc(datetime.fromisoformat(saved["start"]))
            # Relative windows move forward between invocations. Reuse coverage
            # that starts earlier, then trim cached issues to the requested window.
            if saved.get("project") != project or saved_start > start_dt:
                raise ValueError(
                    f"{project}: checkpoint covers a different start/project; rerun without --resume"
                )
            saved_end = as_utc(datetime.fromisoformat(saved["end"]))
            if saved_end > end_dt:
                raise ValueError(
                    f"{project}: checkpoint ends after the requested window"
                )
            issues = filter_issue_window(project, saved["issues"], start_dt, saved_end)
            for i, issue in enumerate(issues):
                if (
                    issue.get("assets_cloud_id") != CLOUD_ID
                    or issue.get("assets_workspace_id") != WORKSPACE_ID
                    or issue.get("asset_errors")
                ):
                    issues[i] = enrich_issue_assets(issue)
            fetch_start = max(start_dt, saved_end)
    # Progress from a fetch that was interrupted partway. Because the JQL sorts
    # created DESC, a partial log covers the NEWEST slice of the window, so what
    # is left to fetch is everything older than the oldest issue it holds.
    partial, oldest_partial = ([], None)
    if resume:
        partial, oldest_partial = load_partial(project, start_dt, end_dt)
        if partial:
            print(
                f"[{project}] resuming: {len(partial)} issues already saved, "
                f"oldest {oldest_partial.isoformat()}",
                flush=True,
            )

    started = time.time()
    windows = []
    if oldest_partial is None:
        windows.append((fetch_start, end_dt))
    else:
        # The older remainder. The boundary issue itself is refetched and then
        # deduped, which is cheaper than trying to express an exclusive bound
        # through Jira's minute-granularity search.
        if fetch_start < oldest_partial:
            windows.append((fetch_start, oldest_partial))
        # A sliver at the top if the requested end moved on since the log began.
        if fetch_start > oldest_partial:
            windows.append((fetch_start, end_dt))

    append = (
        begin_partial(project, start_dt, end_dt)
        if not partial
        else _reopen_partial(project)
    )
    fresh = []
    for window_start, window_end in windows:
        print(
            f"[{project}] fetching {window_start.isoformat()} -> {window_end.isoformat()}",
            flush=True,
        )
        fresh.extend(
            fetch_jira_issues(
                window_start,
                window_end,
                max_issues=1_000_000,
                project=project,
                save_path=None,
                progress_cb=_make_progress(project, started),
                partial_cb=append,
                partial_every=SAVE_EVERY,
            )
        )

    issues = dedupe_issues(issues + partial + fresh)
    issues = filter_issue_window(project, issues, start_dt, end_dt)
    write_json_atomic(
        checkpoint,
        {
            "version": 2,
            "project": project,
            "start": start_dt.isoformat(),
            "end": end_dt.isoformat(),
            "assets_cloud_id": CLOUD_ID,
            "assets_workspace_id": WORKSPACE_ID,
            "issues": issues,
        },
    )
    clear_partial(project)
    print(f"[{project}] {len(issues)} issues checkpointed", flush=True)
    return issues


def merge_into_pickle(frames):
    """Upsert freshly-loaded frames into the existing pickle, with a backup."""
    from data_transformation import upsert_jira_data

    df_old = load_data()
    if df_old is None:
        df_old = pd.DataFrame()

    df_combined = pd.concat(frames, ignore_index=True)

    df = upsert_jira_data(df_old, df_combined)

    if "clone_in_project" not in df.columns:
        df["clone_in_project"] = "-"

    if os.path.exists(DATA_PATH):
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        backup = f"{DATA_PATH}.bak-{stamp}"
        shutil.copy2(DATA_PATH, backup)
        print(f"backed up existing pickle -> {backup}", flush=True)

    save_data(df)
    return df_old, df


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    window = ap.add_mutually_exclusive_group()
    window.add_argument("--days", type=int, help="how far back to fetch (default: 365)")
    window.add_argument(
        "--start",
        type=date.fromisoformat,
        help="first creation date, YYYY-MM-DD (Berlin)",
    )
    ap.add_argument(
        "--end",
        type=date.fromisoformat,
        help=(
            "last creation date, YYYY-MM-DD (Berlin), inclusive; default is now. "
            "Use with --start to fetch one bounded slice and upsert it, instead "
            "of refetching a whole window that is already collected."
        ),
    )
    ap.add_argument(
        "--project",
        action="append",
        choices=list(BY_KEY),
        help="limit to one project (repeatable); default is all three",
    )
    ap.add_argument(
        "--resume",
        action="store_true",
        help="reuse matching checkpoints and fetch the remaining interval",
    )
    args = ap.parse_args(argv)
    if args.days is not None and args.days <= 0:
        ap.error("--days must be positive")
    return args


def main(argv=None):
    from data_transformation import load_project_issues

    args = parse_args(argv)
    projects = list(dict.fromkeys(args.project or BY_KEY))
    # dt_time.max makes --end inclusive of the whole Berlin day, matching --start
    # which begins at that day's midnight.
    end_dt = (
        as_utc(
            datetime.combine(args.end, dt_time.max, tzinfo=ZoneInfo("Europe/Berlin"))
        )
        if args.end
        else datetime.now(UTC)
    )
    start_dt = (
        datetime.combine(args.start, dt_time.min, tzinfo=ZoneInfo("Europe/Berlin"))
        if args.start
        else end_dt - timedelta(days=args.days or 365)
    )
    start_dt = as_utc(start_dt)
    if start_dt > end_dt:
        raise SystemExit(
            "Start date is after the end date"
            if args.end
            else "Start date is in the future"
        )
    print(
        f"Backfilling {', '.join(projects)} ({start_dt.date()} -> {end_dt.date()})",
        flush=True,
    )

    overall = time.time()
    frames = []
    errors = []
    for project in projects:
        try:
            issues = fetch_project(project, start_dt, end_dt, args.resume)
            frame = load_project_issues(project, issues) if issues else None
        except Exception as exc:  # noqa: BLE001 - report optional/isolated failures
            errors.append(project)
            print(f"[{project}] failed: {exc}", flush=True)
            continue
        asset_failures = sum(bool(issue.get("asset_errors")) for issue in issues)
        if asset_failures:
            print(
                f"[{project}] WARNING: incomplete normal Assets enrichment for {asset_failures} tickets",
                flush=True,
            )
        if not issues:
            print(f"[{project}] no issues returned; skipping", flush=True)
            continue
        frames.append(frame)

    if not frames:
        print("Nothing fetched — pickle left untouched.")
        return 1 if errors else 0

    df_old, df = merge_into_pickle(frames)

    print(f"\nMerged: {len(df_old)} existing -> {len(df)} rows in {DATA_PATH}")
    if "created" in df.columns:
        created = pd.to_datetime(df["created"], errors="coerce", utc=True)
        print(f"created range: {created.min()} -> {created.max()}")
    print(f"total time: {_fmt_duration(time.time() - overall)}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
