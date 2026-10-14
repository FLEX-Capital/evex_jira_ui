import importlib
import json
import tempfile
import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

with patch("jira.JIRA"):
    backfill = importlib.import_module("backfill_jira")


class BackfillTests(unittest.TestCase):
    def test_relative_window_resume_accepts_elapsed_time(self):
        for project in ("SDIPR", "SDAX"):
            for window in ([], ["--days", "60"]):
                with (
                    self.subTest(project=project, window=window),
                    tempfile.TemporaryDirectory() as tmp,
                    patch.object(backfill, "CHECKPOINT_TMPL", tmp + "/{project}.json"),
                    patch.object(
                        backfill, "fetch_jira_issues", return_value=[]
                    ) as fetch,
                    patch.object(backfill, "datetime", wraps=datetime) as clock,
                ):
                    clock.now.return_value = datetime(2026, 9, 10, tzinfo=UTC)
                    self.assertEqual(backfill.main(["--project", project, *window]), 0)
                    clock.now.return_value = datetime(2026, 9, 10, 0, 0, 1, tzinfo=UTC)
                    self.assertEqual(
                        backfill.main(["--project", project, *window, "--resume"]), 0
                    )
                    self.assertEqual(
                        fetch.call_args.args[0], datetime(2026, 9, 10, tzinfo=UTC)
                    )
                    saved = json.loads((Path(tmp) / f"{project}.json").read_text())
                    self.assertEqual(saved["end"], "2026-09-10T00:00:01+00:00")

    def test_resume_reuses_coverage_but_rejects_expansion(self):
        start = datetime(2026, 9, 1, tzinfo=UTC)
        end = datetime(2026, 9, 10, tzinfo=UTC)
        issues = [
            {
                "key": f"SDAX-{day}",
                "fields": {"created": f"2026-09-{day:02d}T00:00:00Z"},
                "assets_cloud_id": backfill.CLOUD_ID,
                "assets_workspace_id": backfill.WORKSPACE_ID,
            }
            for day in (1, 2)
        ]
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(backfill, "CHECKPOINT_TMPL", tmp + "/{project}.json"),
            patch.object(backfill, "fetch_jira_issues", return_value=issues),
        ):
            backfill.fetch_project("SDAX", start, end, False)
            with self.assertRaisesRegex(ValueError, "start/project"):
                backfill.fetch_project("SDAX", start - timedelta(days=1), end, True)
            with patch.object(backfill, "fetch_jira_issues", return_value=[]):
                result = backfill.fetch_project(
                    "SDAX",
                    start + timedelta(seconds=1),
                    end + timedelta(seconds=1),
                    True,
                )
            self.assertEqual([i["key"] for i in result], ["SDAX-2"])

    def test_explicit_start_conflicts_with_days(self):
        with self.assertRaises(SystemExit):
            backfill.parse_args(["--start", "2026-09-01", "--days", "7"])
        args = backfill.parse_args(["--project", "SDEU", "--start", "2026-09-01"])
        self.assertEqual(args.project, ["SDEU"])
        self.assertEqual(args.start.isoformat(), "2026-09-01")

    def test_checkpoint_records_effective_cutoff_and_normal_workspace(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(backfill, "CHECKPOINT_TMPL", tmp + "/{project}.json"),
            patch.object(backfill, "fetch_jira_issues", return_value=[]) as fetch,
        ):
            backfill.fetch_project(
                "SDEU",
                datetime(2025, 1, 1, tzinfo=UTC),
                datetime(2026, 9, 10, tzinfo=UTC),
                False,
            )
            self.assertEqual(
                fetch.call_args.args[0].isoformat(), "2026-08-31T22:00:00+00:00"
            )
            saved = json.loads((Path(tmp) / "SDEU.json").read_text())
            self.assertEqual(
                saved["assets_workspace_id"], "9926cb30-3f07-4fb2-9c83-aa4fc551c721"
            )
            self.assertEqual(saved["start"], "2026-08-31T22:00:00+00:00")

    def test_legacy_checkpoint_cannot_import_pre_cutoff_or_sandbox_labels(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(backfill, "CHECKPOINT_TMPL", tmp + "/{project}.json"),
        ):
            checkpoint = Path(tmp) / "SDEU.json"
            checkpoint.write_text(
                json.dumps(
                    {
                        "start": "2025-01-01",
                        "end": "2026-09-10",
                        "issues": [
                            {
                                "key": "SDEU-1",
                                "fields": {"created": "2026-07-30T00:00:00Z"},
                                "asset_labels": {"1": "sandbox"},
                            }
                        ],
                    }
                )
            )
            # Old date-only checkpoints cannot establish exact coverage/provenance:
            # the actual Jira window must be fetched again.
            with patch.object(backfill, "fetch_jira_issues", return_value=[]) as fetch:
                issues = backfill.fetch_project(
                    "SDEU",
                    datetime(2025, 1, 1, tzinfo=UTC),
                    datetime(2026, 9, 10, tzinfo=UTC),
                    True,
                )
            self.assertEqual(issues, [])
            fetch.assert_called_once()

    def test_resume_filters_old_rows_and_fetches_tail_without_duplicates(self):
        start = datetime(2026, 8, 31, 22, tzinfo=UTC)
        end = datetime(2026, 9, 10, tzinfo=UTC)
        earlier = datetime(2026, 9, 9, tzinfo=UTC)

        def issue(key, created, updated):
            return {
                "key": key,
                "fields": {"created": created, "updated": updated},
                "assets_cloud_id": backfill.CLOUD_ID,
                "assets_workspace_id": backfill.WORKSPACE_ID,
            }

        old = issue("SDEU-1", "2026-09-09T00:00:00Z", "2026-09-09T00:00:00Z")
        fresh = issue("SDEU-1", "2026-09-09T00:00:00Z", "2026-09-10T00:00:00Z")
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(backfill, "CHECKPOINT_TMPL", tmp + "/{project}.json"),
        ):
            (Path(tmp) / "SDEU.json").write_text(
                json.dumps(
                    {
                        "version": 2,
                        "project": "SDEU",
                        "start": start.isoformat(),
                        "end": earlier.isoformat(),
                        "issues": [
                            old,
                            issue(
                                "SDEU-0", "2026-07-30T00:00:00Z", "2026-08-01T00:00:00Z"
                            ),
                        ],
                    }
                )
            )
            with patch.object(
                backfill, "fetch_jira_issues", return_value=[fresh]
            ) as fetch:
                result = backfill.fetch_project("SDEU", start, end, True)
            self.assertEqual(fetch.call_args.args[0], earlier)
            self.assertEqual([i["key"] for i in result], ["SDEU-1"])
            self.assertEqual(result[0]["fields"]["updated"], "2026-09-10T00:00:00Z")


if __name__ == "__main__":
    unittest.main()


class PartialResumeTests(unittest.TestCase):
    """An interrupted fetch must resume from its partial log, not from zero."""

    def setUp(self):
        self.tmp = self.enterContext(tempfile.TemporaryDirectory())
        # Redirecting the checkpoint redirects the partial log with it.
        self.enterContext(
            patch.object(backfill, "CHECKPOINT_TMPL", self.tmp + "/{project}.json")
        )
        self.start = datetime(2025, 3, 17, tzinfo=UTC)
        self.end = datetime(2026, 9, 16, tzinfo=UTC)

    @staticmethod
    def _issue(day):
        return {
            "key": f"SDAX-{day}",
            "fields": {"created": f"2026-08-{day:02d}T00:00:00+00:00"},
            "assets_cloud_id": backfill.CLOUD_ID,
            "assets_workspace_id": backfill.WORKSPACE_ID,
        }

    def _write_partial(self, issues):
        append = backfill.begin_partial("SDAX", self.start, self.end)
        append(issues)

    def test_interrupted_fetch_leaves_a_resumable_log(self):
        self._write_partial([self._issue(10), self._issue(11)])
        loaded, oldest = backfill.load_partial("SDAX", self.start, self.end)
        self.assertEqual(sorted(i["key"] for i in loaded), ["SDAX-10", "SDAX-11"])
        self.assertEqual(oldest, datetime(2026, 8, 10, tzinfo=UTC))

    def test_resume_fetches_only_the_older_remainder(self):
        self._write_partial([self._issue(10), self._issue(11)])
        with patch.object(backfill, "fetch_jira_issues", return_value=[]) as fetch:
            backfill.fetch_project("SDAX", self.start, self.end, True)
        # DESC ordering means the log holds the newest slice, so the refetch
        # must cover [start, oldest] - not the whole window again.
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(fetch.call_args.args[0], self.start)
        self.assertEqual(fetch.call_args.args[1], datetime(2026, 8, 10, tzinfo=UTC))

    def test_resumed_issues_survive_into_the_final_checkpoint(self):
        self._write_partial([self._issue(10), self._issue(11)])
        with patch.object(backfill, "fetch_jira_issues", return_value=[self._issue(5)]):
            issues = backfill.fetch_project("SDAX", self.start, self.end, True)
        self.assertEqual(
            sorted(i["key"] for i in issues), ["SDAX-10", "SDAX-11", "SDAX-5"]
        )

    def test_completed_fetch_clears_the_partial_log(self):
        self._write_partial([self._issue(10)])
        with patch.object(backfill, "fetch_jira_issues", return_value=[]):
            backfill.fetch_project("SDAX", self.start, self.end, True)
        log, meta = backfill._partial_paths("SDAX")
        self.assertFalse(Path(log).exists())
        self.assertFalse(Path(meta).exists())

    def test_torn_final_line_costs_one_issue_not_the_whole_log(self):
        self._write_partial([self._issue(10), self._issue(11)])
        log, _ = backfill._partial_paths("SDAX")
        with open(log, "a") as handle:
            handle.write('{"key": "SDAX-12", "fields": {"crea')
        loaded, oldest = backfill.load_partial("SDAX", self.start, self.end)
        self.assertEqual(sorted(i["key"] for i in loaded), ["SDAX-10", "SDAX-11"])
        self.assertEqual(oldest, datetime(2026, 8, 10, tzinfo=UTC))

    def test_log_from_a_different_window_is_ignored_not_merged(self):
        self._write_partial([self._issue(10)])
        loaded, oldest = backfill.load_partial(
            "SDAX", self.start + timedelta(days=1), self.end
        )
        self.assertEqual(loaded, [])
        self.assertIsNone(oldest)

    def test_without_resume_the_stale_log_is_discarded(self):
        self._write_partial([self._issue(10), self._issue(11)])
        with patch.object(backfill, "fetch_jira_issues", return_value=[]) as fetch:
            issues = backfill.fetch_project("SDAX", self.start, self.end, False)
        self.assertEqual(issues, [])
        self.assertEqual(fetch.call_args.args[0], self.start)
        self.assertEqual(fetch.call_args.args[1], self.end)


class PartialPathIsolationTests(unittest.TestCase):
    """Regression: the suite once deleted a live backfill's partial log.

    The partial paths were their own module constants, so every test that
    patched only CHECKPOINT_TMPL still ran begin_partial/clear_partial against
    the real data/ directory.
    """

    def test_partial_paths_follow_the_patched_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(backfill, "CHECKPOINT_TMPL", tmp + "/{project}.json"):
                log, meta = backfill._partial_paths("SDAX")
            self.assertTrue(log.startswith(tmp), log)
            self.assertTrue(meta.startswith(tmp), meta)

    def test_default_partial_paths_sit_beside_the_default_checkpoint(self):
        log, meta = backfill._partial_paths("SDAX")
        self.assertEqual(log, "data/backfill_SDAX.partial.ndjson")
        self.assertEqual(meta, "data/backfill_SDAX.partial.json")

    def test_running_main_never_writes_outside_the_patched_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            before = set(Path("data").iterdir()) if Path("data").exists() else set()
            with (
                patch.object(backfill, "CHECKPOINT_TMPL", tmp + "/{project}.json"),
                patch.object(backfill, "fetch_jira_issues", return_value=[]),
                patch.object(backfill, "merge_into_pickle", return_value=(None, None)),
            ):
                backfill.main(["--project", "SDAX", "--days", "7"])
            after = set(Path("data").iterdir()) if Path("data").exists() else set()
            self.assertEqual(before, after)


class EndDateTests(unittest.TestCase):
    """--end bounds the window so an existing collection can be extended cheaply."""

    def test_end_is_inclusive_of_the_whole_berlin_day(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(backfill, "CHECKPOINT_TMPL", tmp + "/{project}.json"),
            patch.object(backfill, "fetch_jira_issues", return_value=[]) as fetch,
            patch.object(backfill, "merge_into_pickle", return_value=(None, None)),
        ):
            backfill.main(
                ["--project", "SDAX", "--start", "2025-01-01", "--end", "2025-03-17"]
            )
        start, end = fetch.call_args.args[0], fetch.call_args.args[1]
        self.assertEqual(start, datetime(2024, 12, 31, 23, tzinfo=UTC))
        # 2025-03-17 23:59:59.999999 Berlin == 22:59:59.999999 UTC
        self.assertEqual(end.astimezone(UTC).hour, 22)
        self.assertEqual(end.date(), date(2025, 3, 17))

    def test_without_end_the_window_still_runs_to_now(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(backfill, "CHECKPOINT_TMPL", tmp + "/{project}.json"),
            patch.object(backfill, "fetch_jira_issues", return_value=[]) as fetch,
            patch.object(backfill, "merge_into_pickle", return_value=(None, None)),
            patch.object(backfill, "datetime", wraps=datetime) as clock,
        ):
            clock.now.return_value = datetime(2026, 9, 16, tzinfo=UTC)
            backfill.main(["--project", "SDAX", "--days", "7"])
        self.assertEqual(fetch.call_args.args[1], datetime(2026, 9, 16, tzinfo=UTC))

    def test_days_counts_back_from_an_explicit_end(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(backfill, "CHECKPOINT_TMPL", tmp + "/{project}.json"),
            patch.object(backfill, "fetch_jira_issues", return_value=[]) as fetch,
            patch.object(backfill, "merge_into_pickle", return_value=(None, None)),
        ):
            backfill.main(["--project", "SDAX", "--days", "10", "--end", "2025-03-17"])
        span = fetch.call_args.args[1] - fetch.call_args.args[0]
        self.assertEqual(span.days, 10)

    def test_start_after_end_is_rejected(self):
        with self.assertRaises(SystemExit):
            backfill.main(
                ["--project", "SDAX", "--start", "2026-01-01", "--end", "2025-01-01"]
            )
