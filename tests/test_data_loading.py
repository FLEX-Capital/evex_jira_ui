import multiprocessing
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import data_loading
from settings import load_settings


def _write_from_another_process(path, entered, done):
    entered.set()
    data_loading.save_data(pd.DataFrame({"key": ["CLI-1"]}), path=path)
    done.set()


class PersistenceTests(unittest.TestCase):
    def test_transaction_blocks_a_separate_cli_process(self):
        context = multiprocessing.get_context("spawn")
        entered, done = context.Event(), context.Event()
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"DATA_DIR": tmp}),
        ):
            child = context.Process(
                target=_write_from_another_process,
                args=(load_settings().paths.tickets, entered, done),
            )
            try:
                with data_loading.cache_lock():
                    child.start()
                    self.assertTrue(entered.wait(5))
                    self.assertFalse(done.wait(0.1))
                child.join(5)
                self.assertTrue(done.is_set())
                self.assertEqual(child.exitcode, 0)
            finally:
                if child.is_alive():
                    child.terminate()
                    child.join(3)

    def test_stale_snapshot_cannot_overwrite_a_later_save(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"DATA_DIR": tmp}),
        ):
            data_loading.save_data(pd.DataFrame({"key": ["SDAX-1"]}))
            snapshot, revision = data_loading.load_data_snapshot()
            data_loading.save_data(pd.DataFrame({"key": ["SDAX-1", "SDEU-2"]}))
            with self.assertRaises(data_loading.CacheChangedError):
                data_loading.save_data_if_unchanged(snapshot, revision)
            self.assertEqual(
                data_loading.load_data()["key"].tolist(), ["SDAX-1", "SDEU-2"]
            )

    def test_transaction_serializes_competing_writer(self):
        entered, done = threading.Event(), threading.Event()
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"DATA_DIR": tmp}),
        ):

            def writer():
                entered.set()
                data_loading.save_data(pd.DataFrame({"key": ["SDAX-2"]}))
                done.set()

            with data_loading.cache_lock():
                data_loading.save_data(pd.DataFrame({"key": ["SDAX-1"]}))
                thread = threading.Thread(target=writer)
                thread.start()
                self.assertTrue(entered.wait(2))
                self.assertFalse(done.wait(0.1))
                self.assertEqual(data_loading.load_data()["key"].tolist(), ["SDAX-1"])
            thread.join(3)
            self.assertTrue(done.is_set())
            self.assertEqual(data_loading.load_data()["key"].tolist(), ["SDAX-2"])

    def test_failed_serialization_keeps_previous_cache(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"DATA_DIR": tmp}),
        ):
            data_loading.save_data(pd.DataFrame({"key": ["SDAX-1"]}))
            previous = Path(load_settings().paths.tickets).read_bytes()
            with (
                patch.object(
                    data_loading.pickle,
                    "dump",
                    side_effect=RuntimeError("write failed"),
                ),
                self.assertRaises(RuntimeError),
            ):
                data_loading.save_data(pd.DataFrame())
            self.assertEqual(Path(load_settings().paths.tickets).read_bytes(), previous)


if __name__ == "__main__":
    unittest.main()
