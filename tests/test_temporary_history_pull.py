import tempfile
import threading
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from streamlit.testing.v1 import AppTest

import temporary_history_pull as history


class HistoryPullTests(unittest.TestCase):
    def test_full_window_all_desks_and_partial_failure_keep_history(self):
        calls = []

        def fetch(start, end, **kwargs):
            project = kwargs["project"]
            calls.append((project, start, end, kwargs))
            if project == "SDAX":
                raise RuntimeError("unavailable")
            kwargs["progress_cb"](1)
            return [{"key": project + "-1", "asset_errors": ["HTTP 403"]}]

        def transform(project, issues):
            return pd.DataFrame({"key": [issues[0]["key"]], "value": [2]})

        end = datetime(2026, 9, 21, tzinfo=UTC)
        result = history.pull_history(fetch=fetch, transform=transform, end=end)
        self.assertEqual([c[0] for c in calls], ["SDIPR", "SDAX", "SDEU"])
        self.assertEqual(calls[0][1].isoformat(), "2024-12-31T23:00:00+00:00")
        self.assertTrue(
            all(c[1].isoformat() == "2024-12-31T23:00:00+00:00" for c in calls)
        )
        self.assertTrue(all(c[2] == end for c in calls))
        self.assertTrue(all(c[3]["save_path"] is None for c in calls))
        self.assertEqual(set(result.errors), {"SDAX"})
        self.assertEqual(result.asset_failures, {"SDIPR": 1, "SDEU": 1})
        self.assertEqual(set(result.frame["key"]), {"SDIPR-1", "SDEU-1"})

    def test_background_fetch_does_not_write_and_duplicate_start_is_ignored(self):
        started, release = threading.Event(), threading.Event()
        runs = []

        def pull(**kwargs):
            runs.append(1)
            started.set()
            release.wait(5)
            return history.RefreshResult(pd.DataFrame())

        job = history.HistoryJob()
        with patch.object(history, "pull_history", side_effect=pull):
            try:
                self.assertTrue(job.start())
                self.assertTrue(started.wait(2))
                self.assertFalse(job.start())
                self.assertEqual(job.snapshot()["state"], "running")
            finally:
                release.set()
                job.thread.join(3)
        self.assertEqual(len(runs), 1)
        self.assertEqual(job.snapshot()["state"], "ready")

    def test_apply_uses_latest_cache_backs_up_and_is_idempotent(self):
        import data_loading

        result = history.RefreshResult(
            pd.DataFrame({"key": ["SDIPR-1", "SDEU-1"], "value": [2, 3]}),
            counts={"SDIPR": 1, "SDEU": 1},
        )
        job = history.HistoryJob()
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(data_loading, "DATA_PATH", str(Path(tmp) / "cache.pkl")),
            patch.object(history, "pull_history", return_value=result),
        ):
            job.start()
            job.thread.join(3)
            self.assertFalse(Path(data_loading.DATA_PATH).exists())
            # Another maintenance action saves a row while the pull is running.
            current = pd.DataFrame({"key": ["SDIPR-1", "SDAX-old"], "value": [1, 9]})
            data_loading.save_data(current)
            before = Path(data_loading.DATA_PATH).read_bytes()
            self.assertTrue(job.apply())
            self.assertFalse(job.apply())
            saved = data_loading.load_data().set_index("key")
            self.assertEqual(
                saved["value"].to_dict(), {"SDIPR-1": 2, "SDAX-old": 9, "SDEU-1": 3}
            )
            backups = list(Path(tmp).glob("*.bak-history-*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), before)

    def test_all_failure_never_writes_cache(self):
        def fetch(*args, **kwargs):
            raise RuntimeError("offline")

        result = history.pull_history(fetch=fetch, transform=lambda *a: None)
        job = history.HistoryJob()
        with (
            patch.object(history, "pull_history", return_value=result),
            patch("data_loading.save_data") as save,
        ):
            job.start()
            job.thread.join(3)
            self.assertTrue(job.apply())
            save.assert_not_called()
        self.assertEqual(len(job.snapshot()["errors"]), 3)

    def test_first_import_creates_missing_cache(self):
        import data_loading

        result = history.RefreshResult(pd.DataFrame({"key": ["SDIPR-1"]}))
        job = history.HistoryJob()
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(data_loading, "DATA_PATH", str(Path(tmp) / "cache.pkl")),
            patch.object(history, "pull_history", return_value=result),
        ):
            job.start()
            job.thread.join(3)
            job.apply()
            self.assertEqual(data_loading.load_data()["key"].tolist(), ["SDIPR-1"])

    def test_concurrent_cache_change_keeps_result_retryable(self):
        import data_loading

        incoming = pd.DataFrame({"key": ["SDIPR-1"]})
        concurrent = pd.DataFrame({"key": ["SDAX-1"]})
        job = history.HistoryJob()
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(data_loading, "DATA_PATH", str(Path(tmp) / "cache.pkl")),
            patch.object(
                history, "pull_history", return_value=history.RefreshResult(incoming)
            ),
        ):
            data_loading.save_data(incoming)
            job.start()
            job.thread.join(3)

            def load_and_modify():
                data_loading.save_data(concurrent)
                return incoming.copy()

            with (
                patch.object(data_loading, "load_data", side_effect=load_and_modify),
                self.assertRaisesRegex(RuntimeError, "parallel"),
            ):
                job.apply()
            self.assertEqual(job.snapshot()["state"], "ready")
            self.assertEqual(data_loading.load_data()["key"].tolist(), ["SDAX-1"])
            self.assertEqual(list(Path(tmp).glob("*.bak-*")), [])
            job.apply()
            self.assertEqual(
                set(data_loading.load_data()["key"]), {"SDIPR-1", "SDAX-1"}
            )


class HistoryUITests(unittest.TestCase):
    def test_finished_import_reloads_cache_and_reports_partial_assets(self):
        from datetime import date

        from test_transformation import issue

        import data_loading
        from data_transformation import load_project_issues

        frame = load_project_issues("SDIPR", [issue(key="SDIPR-1")])
        job = history.HistoryJob()
        result = history.RefreshResult(
            frame,
            counts={"SDIPR": 1},
            errors={"SDAX": "unavailable"},
            asset_failures={"SDIPR": 1},
        )
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(data_loading, "DATA_PATH", str(Path(tmp) / "cache.pkl")),
            patch.object(history, "JOB", job),
            patch.object(history, "pull_history", return_value=result),
            patch("interactive.render_interactive"),
        ):
            job.start()
            job.thread.join(3)
            app = AppTest.from_file("app.py", default_timeout=30).run()
            app.sidebar.date_input[0].set_value(
                (date(2025, 1, 1), date(2026, 9, 21))
            ).run()
            self.assertFalse(app.exception)
            self.assertEqual(app.dataframe[0].value["key"].tolist(), ["SDIPR-1"])
            self.assertEqual(job.snapshot()["state"], "completed")
            self.assertTrue(any("unvollständig" in w.value for w in app.warning))
            self.assertTrue(any("SDAX" in e.value for e in app.error))
            self.assertEqual(len(list(Path(tmp).glob("*.bak-*"))), 0)

    def test_button_available_with_empty_cache_and_shows_background_progress(self):
        job = history.HistoryJob()
        release = threading.Event()

        def pull(**kwargs):
            release.wait(5)
            return history.RefreshResult(pd.DataFrame())

        with (
            patch.object(history, "JOB", job),
            patch.object(history, "pull_history", side_effect=pull),
            patch("data_loading.load_data", return_value=pd.DataFrame()),
        ):
            try:
                app = AppTest.from_file("app.py", default_timeout=30).run()
                buttons = [b for b in app.button if "01.01.2025" in b.label]
                self.assertEqual(len(buttons), 1)
                buttons[0].click().run()
                self.assertFalse(app.exception)
                self.assertTrue(
                    next(b for b in app.button if "01.01.2025" in b.label).disabled
                )
                self.assertTrue(any("Hintergrund" in i.value for i in app.info))
            finally:
                release.set()
                if job.thread:
                    job.thread.join(3)
