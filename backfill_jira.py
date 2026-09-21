"""Backfill ``data/jira_data.pkl`` with a full range of SDIPR, SDAX, and SDEU issues.

Why this exists instead of the sidebar button: the Streamlit refresh runs the
fetch inline in a script run, so any browser interaction or reconnect reruns
the script and kills the fetch mid-flight. A year of issues takes long enough
that this is close to guaranteed. Run this from a terminal instead.

    uv run python backfill_jira.py                  # past year, all desks
    uv run python backfill_jira.py --days 60        # shorter window
    uv run python backfill_jira.py --resume         # reuse existing checkpoints
    uv run python backfill_jira.py --project SDIPR  # one project only

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
    started = time.time()
    print(
        f"[{project}] fetching {fetch_start.isoformat()} -> {end_dt.isoformat()}",
        flush=True,
    )
    fresh = fetch_jira_issues(
        fetch_start,
        end_dt,
        max_issues=1_000_000,
        project=project,
        save_path=None,
        progress_cb=_make_progress(project, started),
    )
    issues = dedupe_issues(issues + fresh)
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
    end_dt = datetime.now(UTC)
    start_dt = (
        datetime.combine(args.start, dt_time.min, tzinfo=ZoneInfo("Europe/Berlin"))
        if args.start
        else end_dt - timedelta(days=args.days or 365)
    )
    start_dt = as_utc(start_dt)
    if start_dt > end_dt:
        raise SystemExit("Start date is in the future")
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
