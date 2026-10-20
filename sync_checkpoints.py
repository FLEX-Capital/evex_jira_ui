"""Disk checkpoints and interrupted-download recovery shared by every importer."""

import json
import os
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from desk_sync import write_json_atomic
from jira_loader import (
    dedupe_issues,
    enrich_issue_assets,
    fetch_jira_issues,
)
from service_desks import InvalidIssue, as_utc, effective_start, filter_issue_window
from settings import DEFAULT_TENANT, Settings, load_settings


class CheckpointStore:
    def __init__(
        self,
        checkpoint_dir: str | Path | None = None,
        *,
        progress: Callable[[str], None] | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or load_settings()
        self.tenant = self.settings.tenant
        checkpoint_dir = (
            self.settings.paths.root if checkpoint_dir is None else checkpoint_dir
        )
        self.template = str(Path(checkpoint_dir) / "backfill_{project}.json")
        self.save_every = 100
        self.progress = progress
        self.fetch_jira_issues = fetch_jira_issues

    def _say(self, message, **kwargs):
        if self.progress:
            self.progress(message)
        else:
            print(message, **kwargs)

    def _partial_paths(self, project):
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
        base = self.template.format(project=project)
        stem = base.removesuffix(".json")
        return (f"{stem}.partial.ndjson", f"{stem}.partial.json")

    def begin_partial(self, project, start_dt, end_dt):
        """Start a fresh partial log, returning an appender for fetch_jira_issues.

        The metadata is written first and separately: a partial log is only safe to
        resume from if we know which window produced it, and that has to be on disk
        before the first issue is, not after.
        """
        log_path, meta_path = self._partial_paths(project)
        os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
        write_json_atomic(
            meta_path,
            {
                "version": 1,
                "project": project,
                "start": start_dt.isoformat(),
                "end": end_dt.isoformat(),
                "assets_cloud_id": self.tenant.cloud_id,
                "assets_workspace_id": self.tenant.workspace_id,
            },
        )
        open(log_path, "w").close()

        def append(issues):
            with open(log_path, "a") as handle:
                handle.writelines((json.dumps(issue) + "\n" for issue in issues))
                handle.flush()
                os.fsync(handle.fileno())
            self._say(f"  [{project}] checkpointed +{len(issues)} issues", flush=True)

        return append

    def _reopen_partial(self, project):
        """Appender for a partial log that already exists, leaving its lines intact."""
        log_path, _ = self._partial_paths(project)
        # A new append must not follow a torn JSON fragment or concatenate with
        # a complete record whose newline was interrupted. Keep the valid prefix.
        with open(log_path, "r+b") as handle:
            while True:
                start = handle.tell()
                line = handle.readline()
                if not line:
                    break
                if not line.strip():
                    continue
                try:
                    json.loads(line)
                except (ValueError, UnicodeError):
                    handle.seek(start)
                    handle.truncate()
                    break
            handle.seek(0, os.SEEK_END)
            if handle.tell():
                handle.seek(-1, os.SEEK_END)
                if handle.read(1) != b"\n":
                    handle.write(b"\n")
            handle.flush()
            os.fsync(handle.fileno())

        def append(issues):
            with open(log_path, "a") as handle:
                handle.writelines((json.dumps(issue) + "\n" for issue in issues))
                handle.flush()
                os.fsync(handle.fileno())
            self._say(f"  [{project}] checkpointed +{len(issues)} issues", flush=True)

        return append

    def load_partial(self, project, start_dt, *, invalid_row_cb=None):
        """Return (issues, oldest_created, logged_end) from a resumable partial log.

        Returns ([], None, None) when there is nothing to resume. A log from another
        start, project or Assets workspace is ignored rather than merged: silently
        mixing two windows is how a backfill ends up with a gap it cannot detect.
        The end may differ (a default end of "now" moves between runs); the caller
        fetches anything newer than `logged_end` and trims to its own window.
        """
        log_path, meta_path = self._partial_paths(project)
        if not (os.path.exists(log_path) and os.path.exists(meta_path)):
            return ([], None, None)
        with open(meta_path) as handle:
            meta = json.load(handle)
        if (
            meta.get("project") != project
            or meta.get("assets_cloud_id") != self.tenant.cloud_id
            or meta.get("assets_workspace_id") != self.tenant.workspace_id
            or (as_utc(datetime.fromisoformat(meta["start"])) != start_dt)
        ):
            return ([], None, None)
        issues = []
        with open(log_path) as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    issues.append(json.loads(line))
                except json.JSONDecodeError:
                    self._say(f"  [{project}] discarding torn final line", flush=True)
                    break
        issues = filter_issue_window(
            project, issues, start_dt, None, invalid_row_cb=invalid_row_cb
        )
        issues = dedupe_issues(issues)
        if not issues:
            return ([], None, None)
        oldest = min(
            (
                as_utc(datetime.fromisoformat(issue["fields"]["created"]))
                for issue in issues
            )
        )
        return (issues, oldest, as_utc(datetime.fromisoformat(meta["end"])))

    def clear_partial(self, project):
        for path in self._partial_paths(project):
            if os.path.exists(path):
                os.unlink(path)

    def _fmt_duration(self, seconds):
        seconds = int(seconds)
        h, rem = divmod(seconds, 3600)
        m, s = divmod(rem, 60)
        return f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s"

    def _make_progress(self, project, started):
        """Print a one-line progress update per fetched page."""
        state = {"last": 0.0}

        def report(count):
            now = time.time()
            if now - state["last"] < 5:
                return
            state["last"] = now
            elapsed = now - started
            rate = count / elapsed if elapsed else 0
            self._say(
                f"  [{project}] {count:>6} issues | {self._fmt_duration(elapsed)} elapsed | {rate:.1f} issues/s",
                flush=True,
            )

        return report

    def fetch_project(
        self,
        project,
        start_dt,
        end_dt,
        resume,
        *,
        category_resolver=None,
        invalid_row_cb=None,
    ):
        """Fetch one project's issues, using/creating a raw checkpoint file."""
        start_dt = effective_start(project, start_dt)
        end_dt = as_utc(end_dt)
        if start_dt > end_dt:
            return []
        invalid_count = 0
        retry_incomplete = False

        def record_invalid(event: InvalidIssue) -> None:
            nonlocal invalid_count
            invalid_count += 1
            if invalid_row_cb:
                invalid_row_cb(event)

        checkpoint = self.template.format(project=project)
        issues = []
        fetch_start = start_dt
        if resume and os.path.exists(checkpoint):
            with open(checkpoint) as f:
                saved = json.load(f)
            if saved.get("version") == 2:
                saved_tenant = (
                    saved.get("assets_cloud_id"),
                    saved.get("assets_workspace_id"),
                )
                legacy_default = (
                    saved_tenant == (None, None) and self.tenant == DEFAULT_TENANT
                )
                if not legacy_default and saved_tenant != (
                    self.tenant.cloud_id,
                    self.tenant.workspace_id,
                ):
                    raise ValueError(f"{project}: checkpoint tenant/workspace mismatch")
                saved_start = as_utc(datetime.fromisoformat(saved["start"]))
                if saved.get("project") != project or saved_start > start_dt:
                    raise ValueError(
                        f"{project}: checkpoint covers a different start/project; rerun without --resume"
                    )
                saved_end = as_utc(datetime.fromisoformat(saved["end"]))
                if saved_end > end_dt:
                    raise ValueError(
                        f"{project}: checkpoint ends after the requested window"
                    )
                issues = filter_issue_window(
                    project,
                    saved["issues"],
                    start_dt,
                    saved_end,
                    invalid_row_cb=record_invalid,
                )
                for i, issue in enumerate(issues):
                    if (
                        issue.get("assets_cloud_id") != self.tenant.cloud_id
                        or issue.get("assets_workspace_id") != self.tenant.workspace_id
                        or issue.get("asset_errors")
                    ):
                        issues[i] = enrich_issue_assets(
                            issue,
                            category_resolver=category_resolver,
                            settings=self.settings,
                        )
                retry_incomplete = bool(saved.get("invalid_rows", 0))
                fetch_start = max(start_dt, saved_end)
        partial, oldest_partial, partial_end = ([], None, None)
        if resume:
            partial, oldest_partial, partial_end = self.load_partial(
                project, start_dt, invalid_row_cb=record_invalid
            )
            if partial:
                self._say(
                    f"[{project}] resuming: {len(partial)} issues already saved, oldest {oldest_partial.isoformat()}",
                    flush=True,
                )
        if retry_incomplete or invalid_count:
            # Rejected records have no trustworthy creation time. Retry the
            # entire requested coverage rather than claiming a complete tail.
            fetch_start = start_dt
            oldest_partial = None
        started = time.time()
        windows = []
        if oldest_partial is None:
            windows.append((fetch_start, end_dt))
        else:
            if fetch_start < oldest_partial:
                windows.append((fetch_start, oldest_partial))
            if fetch_start > oldest_partial:
                windows.append((fetch_start, end_dt))
            elif partial_end < end_dt:
                windows.append((partial_end, end_dt))
        append = (
            self.begin_partial(project, start_dt, end_dt)
            if not partial
            else self._reopen_partial(project)
        )
        fresh = []
        for window_start, window_end in windows:
            self._say(
                f"[{project}] fetching {window_start.isoformat()} -> {window_end.isoformat()}",
                flush=True,
            )
            fresh.extend(
                self.fetch_jira_issues(
                    window_start,
                    window_end,
                    max_issues=1000000,
                    project=project,
                    save_path=None,
                    progress_cb=self._make_progress(project, started),
                    partial_cb=append,
                    partial_every=self.save_every,
                    category_resolver=category_resolver,
                    invalid_row_cb=record_invalid,
                    settings=self.settings,
                )
            )
        fresh = filter_issue_window(
            project, fresh, start_dt, end_dt, invalid_row_cb=record_invalid
        )
        issues = dedupe_issues(issues + partial + fresh)
        issues = filter_issue_window(
            project, issues, start_dt, end_dt, invalid_row_cb=record_invalid
        )
        write_json_atomic(
            checkpoint,
            {
                "version": 2,
                "project": project,
                "start": start_dt.isoformat(),
                "end": end_dt.isoformat(),
                "assets_cloud_id": self.tenant.cloud_id,
                "assets_workspace_id": self.tenant.workspace_id,
                "issues": issues,
                "invalid_rows": invalid_count,
            },
        )
        self.clear_partial(project)
        self._say(f"[{project}] {len(issues)} issues checkpointed", flush=True)
        return issues


def fetch_project(
    project,
    start_dt,
    end_dt,
    resume,
    *,
    checkpoint_dir=None,
    category_resolver=None,
    progress=None,
    invalid_row_cb=None,
    settings=None,
):
    return CheckpointStore(
        checkpoint_dir, progress=progress, settings=settings
    ).fetch_project(
        project,
        start_dt,
        end_dt,
        resume,
        category_resolver=category_resolver,
        invalid_row_cb=invalid_row_cb,
    )
