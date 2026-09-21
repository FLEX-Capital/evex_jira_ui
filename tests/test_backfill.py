import importlib
import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
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

    def test_euronet_checkpoint_records_requested_start_and_normal_workspace(self):
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
                fetch.call_args.args[0].isoformat(), "2025-01-01T00:00:00+00:00"
            )
            saved = json.loads((Path(tmp) / "SDEU.json").read_text())
            self.assertEqual(
                saved["assets_workspace_id"], "9926cb30-3f07-4fb2-9c83-aa4fc551c721"
            )
            self.assertEqual(saved["start"], "2025-01-01T00:00:00+00:00")

    def test_legacy_checkpoint_cannot_import_unverified_sandbox_labels(self):
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
