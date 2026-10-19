import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

import jira_loader
from sync_checkpoints import CheckpointStore

START = datetime(2026, 8, 1, tzinfo=UTC)
END = datetime(2026, 9, 1, tzinfo=UTC)


def raw(day):
    return {
        "key": f"SDAX-{day}",
        "fields": {"created": f"2026-08-{day:02d}T00:00:00Z"},
        "assets_cloud_id": jira_loader.CLOUD_ID,
        "assets_workspace_id": jira_loader.WORKSPACE_ID,
    }


def test_partial_resume_fetches_older_remainder_and_new_tail_with_one_resolver(
    tmp_path,
):
    import sync_checkpoints as checkpoints
    from category_sync import CategoryResolver

    store = checkpoints.CheckpointStore(tmp_path)
    store.begin_partial("SDAX", START, END)([raw(10), raw(11)])
    store.fetch_jira_issues = Mock(return_value=[raw(5)])
    resolver = CategoryResolver(fetch=Mock())
    issues = store.fetch_project(
        "SDAX", START, END + timedelta(days=1), True, category_resolver=resolver
    )
    assert {i["key"] for i in issues} == {"SDAX-5", "SDAX-10", "SDAX-11"}
    assert [(c.args[0], c.args[1]) for c in store.fetch_jira_issues.call_args_list] == [
        (START, datetime(2026, 8, 10, tzinfo=UTC)),
        (END, END + timedelta(days=1)),
    ]
    assert all(
        c.kwargs["category_resolver"] is resolver
        for c in store.fetch_jira_issues.call_args_list
    )
    assert not Path(store._partial_paths("SDAX")[0]).exists()


def test_fresh_request_ignores_previous_coverage_and_rejects_resume_expansion(tmp_path):
    import sync_checkpoints as checkpoints

    store = checkpoints.CheckpointStore(tmp_path)
    store.fetch_jira_issues = Mock(return_value=[raw(5)])
    store.fetch_project("SDAX", START, END, False)
    with pytest.raises(ValueError, match="start/project"):
        store.fetch_project("SDAX", START - timedelta(days=1), END, True)
    store.fetch_jira_issues.return_value = [raw(6)]
    fresh = store.fetch_project("SDAX", START, END, False)
    assert [i["key"] for i in fresh] == ["SDAX-6"]
    assert json.loads((tmp_path / "backfill_SDAX.json").read_text())["version"] == 2


def test_torn_partial_tail_does_not_discard_saved_progress(tmp_path):
    import sync_checkpoints as checkpoints

    store = checkpoints.CheckpointStore(tmp_path)
    store.begin_partial("SDAX", START, END)([raw(10)])
    with open(store._partial_paths("SDAX")[0], "a") as handle:
        handle.write('{"key":')
    issues, oldest, _ = store.load_partial("SDAX", START)
    assert [i["key"] for i in issues] == ["SDAX-10"]
    assert oldest == datetime(2026, 8, 10, tzinfo=UTC)
    assert store.load_partial("SDAX", START + timedelta(days=1)) == ([], None, None)


def test_legacy_checkpoint_is_refetched_and_all_paths_follow_explicit_directory(
    tmp_path,
):
    import sync_checkpoints as checkpoints

    (tmp_path / "backfill_SDAX.json").write_text(
        json.dumps({"issues": [raw(10)], "start": "2026-08-01"})
    )
    store = checkpoints.CheckpointStore(tmp_path)
    store.fetch_jira_issues = Mock(return_value=[])
    assert store.fetch_project("SDAX", START, END, True) == []
    assert store.fetch_jira_issues.call_count == 1
    assert all(Path(p).parent == tmp_path for p in store._partial_paths("SDAX"))


@pytest.mark.parametrize(
    "tail,expected",
    [
        ('{"key":', {"SDAX-10", "SDAX-3"}),
        (json.dumps(raw(5)), {"SDAX-10", "SDAX-5", "SDAX-3"}),
    ],
)
def test_second_interruption_does_not_lose_appends_after_a_torn_or_unterminated_tail(
    tmp_path, tail, expected
):
    import sync_checkpoints as checkpoints

    store = checkpoints.CheckpointStore(tmp_path)
    store.begin_partial("SDAX", START, END)([raw(10)])
    with open(store._partial_paths("SDAX")[0], "a") as handle:
        handle.write(tail)

    def interrupted(*args, partial_cb, **kwargs):
        partial_cb([raw(3)])
        raise RuntimeError("second interruption")

    store.fetch_jira_issues = interrupted
    with pytest.raises(RuntimeError):
        store.fetch_project("SDAX", START, END, True)
    issues, _, _ = store.load_partial("SDAX", START)
    assert {i["key"] for i in issues} == expected


backfill = CheckpointStore("data")


class CheckpointCoverageTests(unittest.TestCase):
    def test_resume_reuses_coverage_but_rejects_expansion(self):
        start = datetime(2026, 9, 1, tzinfo=UTC)
        end = datetime(2026, 9, 10, tzinfo=UTC)
        issues = [
            {
                "key": f"SDAX-{day}",
                "fields": {"created": f"2026-09-{day:02d}T00:00:00Z"},
                "assets_cloud_id": jira_loader.CLOUD_ID,
                "assets_workspace_id": jira_loader.WORKSPACE_ID,
            }
            for day in (1, 2)
        ]
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(backfill, "template", tmp + "/{project}.json"),
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

    def test_euronet_checkpoint_records_requested_start_and_normal_workspace(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(backfill, "template", tmp + "/{project}.json"),
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
            patch.object(backfill, "template", tmp + "/{project}.json"),
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
                "assets_cloud_id": jira_loader.CLOUD_ID,
                "assets_workspace_id": jira_loader.WORKSPACE_ID,
            }

        old = issue("SDEU-1", "2026-09-09T00:00:00Z", "2026-09-09T00:00:00Z")
        fresh = issue("SDEU-1", "2026-09-09T00:00:00Z", "2026-09-10T00:00:00Z")
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(backfill, "template", tmp + "/{project}.json"),
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


class PartialResumeTests(unittest.TestCase):
    """An interrupted fetch must resume from its partial log, not from zero."""

    def setUp(self):
        self.tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(
            patch.object(backfill, "template", self.tmp + "/{project}.json")
        )
        self.start = datetime(2025, 3, 17, tzinfo=UTC)
        self.end = datetime(2026, 9, 16, tzinfo=UTC)

    @staticmethod
    def _issue(day):
        return {
            "key": f"SDAX-{day}",
            "fields": {"created": f"2026-08-{day:02d}T00:00:00+00:00"},
            "assets_cloud_id": jira_loader.CLOUD_ID,
            "assets_workspace_id": jira_loader.WORKSPACE_ID,
        }

    def _write_partial(self, issues):
        append = backfill.begin_partial("SDAX", self.start, self.end)
        append(issues)

    def test_interrupted_fetch_leaves_a_resumable_log(self):
        self._write_partial([self._issue(10), self._issue(11)])
        (loaded, oldest, _) = backfill.load_partial("SDAX", self.start)
        self.assertEqual(sorted((i["key"] for i in loaded)), ["SDAX-10", "SDAX-11"])
        self.assertEqual(oldest, datetime(2026, 8, 10, tzinfo=UTC))

    def test_resume_fetches_only_the_older_remainder(self):
        self._write_partial([self._issue(10), self._issue(11)])
        with patch.object(backfill, "fetch_jira_issues", return_value=[]) as fetch:
            backfill.fetch_project("SDAX", self.start, self.end, True)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(fetch.call_args.args[0], self.start)
        self.assertEqual(fetch.call_args.args[1], datetime(2026, 8, 10, tzinfo=UTC))

    def test_resumed_issues_survive_into_the_final_checkpoint(self):
        self._write_partial([self._issue(10), self._issue(11)])
        with patch.object(backfill, "fetch_jira_issues", return_value=[self._issue(5)]):
            issues = backfill.fetch_project("SDAX", self.start, self.end, True)
        self.assertEqual(
            sorted((i["key"] for i in issues)), ["SDAX-10", "SDAX-11", "SDAX-5"]
        )

    def test_moved_end_keeps_the_log_and_fetches_only_what_it_lacks(self):
        oldest = datetime(2026, 8, 10, tzinfo=UTC)
        (later, earlier) = (self.end + timedelta(days=2), self.end - timedelta(days=2))
        cases = {
            later: [(self.start, oldest), (self.end, later)],
            earlier: [(self.start, oldest)],
        }
        for end, windows in cases.items():
            with self.subTest(end=end):
                (Path(self.tmp) / "SDAX.json").unlink(missing_ok=True)
                self._write_partial([self._issue(10)])
                fetched = []

                def fetch(first, last, _fetched=fetched, **kwargs):
                    (log, _) = backfill._partial_paths("SDAX")
                    _fetched.append((first, last, Path(log).read_text().count("\n")))
                    return []

                with patch.object(backfill, "fetch_jira_issues", side_effect=fetch):
                    issues = backfill.fetch_project("SDAX", self.start, end, True)
                self.assertEqual([f[:2] for f in fetched], windows)
                self.assertTrue(all((f[2] == 1 for f in fetched)))
                self.assertEqual([i["key"] for i in issues], ["SDAX-10"])

    def test_completed_fetch_clears_the_partial_log(self):
        self._write_partial([self._issue(10)])
        with patch.object(backfill, "fetch_jira_issues", return_value=[]):
            backfill.fetch_project("SDAX", self.start, self.end, True)
        (log, meta) = backfill._partial_paths("SDAX")
        self.assertFalse(Path(log).exists())
        self.assertFalse(Path(meta).exists())

    def test_torn_final_line_costs_one_issue_not_the_whole_log(self):
        self._write_partial([self._issue(10), self._issue(11)])
        (log, _) = backfill._partial_paths("SDAX")
        with open(log, "a") as handle:
            handle.write('{"key": "SDAX-12", "fields": {"crea')
        (loaded, oldest, _) = backfill.load_partial("SDAX", self.start)
        self.assertEqual(sorted((i["key"] for i in loaded)), ["SDAX-10", "SDAX-11"])
        self.assertEqual(oldest, datetime(2026, 8, 10, tzinfo=UTC))

    def test_log_from_a_different_window_is_ignored_not_merged(self):
        self._write_partial([self._issue(10)])
        (loaded, oldest, _) = backfill.load_partial(
            "SDAX", self.start + timedelta(days=1)
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


class CheckpointPathTests(unittest.TestCase):
    def test_partial_paths_follow_the_patched_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(backfill, "template", tmp + "/{project}.json"):
                (log, meta) = backfill._partial_paths("SDAX")
            self.assertTrue(log.startswith(tmp), log)
            self.assertTrue(meta.startswith(tmp), meta)

    def test_default_partial_paths_sit_beside_the_default_checkpoint(self):
        (log, meta) = backfill._partial_paths("SDAX")
        self.assertEqual(log, "data/backfill_SDAX.partial.ndjson")
        self.assertEqual(meta, "data/backfill_SDAX.partial.json")
