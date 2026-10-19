"""CLI adapter for the shared, resumable Jira import service.

Examples:
    python backfill_jira.py --start 2025-01-01 --project SDAX
    python backfill_jira.py --start 2025-01-01 --end 2025-03-17 --resume

Without --start/--days the past 365 days are fetched for all three desks.
Unknown categories are repaired automatically, including older cached tickets.
"""

import argparse
from datetime import UTC, date, datetime, timedelta

from data_loading import ImportBusyError
from desk_sync import make_sync_request, run_sync
from service_desks import BY_KEY, as_utc, berlin_day_end, berlin_day_start


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
    args = parse_args(argv)
    end = as_utc(berlin_day_end(args.end)) if args.end else datetime.now(UTC)
    start = (
        berlin_day_start(args.start)
        if args.start
        else end - timedelta(days=args.days or 365)
    )
    try:
        request = make_sync_request(start, end, args.project, resume=args.resume)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    print(
        f"Importing {', '.join(request.projects)} ({request.start.isoformat()} -> {request.end.isoformat()})",
        flush=True,
    )
    if args.resume:
        print(
            "Resume reuses compatible raw checkpoints; older checkpointed tickets are not fetched freshly.",
            flush=True,
        )
    try:
        report = run_sync(
            request, progress=lambda event: print(event["message"], flush=True)
        )
    except ImportBusyError as exc:
        print(str(exc), flush=True)
        return 1
    for project in request.projects:
        if project in report.errors:
            print(f"{project}: failed: {report.errors[project]}")
        else:
            print(
                f"{project}: {report.counts.get(project, 0)} tickets; {report.asset_failures.get(project, 0)} Assets warnings"
            )
    categories = report.categories
    print(f"Cache: {report.rows} rows; protected: {report.protected_rows}")
    print(
        f"Categories: {categories.repaired_cells} repaired, {categories.unassigned_cells} unassigned, "
        f"{categories.unresolved_cells} unresolved, {len(categories.failed_objects)} failed references/objects, "
        f"{categories.skipped_cells} concurrent patches skipped"
    )
    for name in ("save_error", "source_error", "raw_error"):
        if getattr(report, name):
            print(f"{name}: {getattr(report, name)}")
    print(
        "Import completed."
        if report.complete
        else "Import incomplete; resolve the reported failures and retry.",
        flush=True,
    )
    return 0 if report.complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
