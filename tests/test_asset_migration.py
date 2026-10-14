import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import asset_migration


class AssetMigrationTests(unittest.TestCase):
    def test_cli_refuses_to_write_when_any_category_lookup_fails(self):
        import data_loading

        frame = pd.DataFrame({"key": ["SDAX-1"], "Hauptkategorie": ["Existing"]})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.pkl"
            frame.to_pickle(path)
            original = path.read_bytes()
            with (
                patch.object(data_loading, "DATA_PATH", str(path)),
                patch.object(
                    asset_migration,
                    "migrate_categories",
                    return_value=(
                        frame.assign(Hauptkategorie="Unbekannt"),
                        {"objects": 2, "failed_objects": 1, "rows": 1},
                    ),
                ),
                patch("sys.argv", ["asset_migration.py"]),
                self.assertRaisesRegex(SystemExit, "Cache unchanged"),
            ):
                asset_migration.main()
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(Path(tmp).glob("*.bak-*")), [])

    def test_cli_success_backs_up_cache_and_preserves_other_fields(self):
        import data_loading

        frame = pd.DataFrame({"key": ["SDAX-1"], "Hauptkategorie": ["Unbekannt"]})
        repaired = frame.assign(Hauptkategorie="Live category")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.pkl"
            frame.to_pickle(path)
            original = path.read_bytes()
            with (
                patch.object(data_loading, "DATA_PATH", str(path)),
                patch.object(
                    asset_migration,
                    "migrate_categories",
                    return_value=(
                        repaired,
                        {"objects": 1, "failed_objects": 0, "rows": 1},
                    ),
                ),
                patch("sys.argv", ["asset_migration.py"]),
            ):
                asset_migration.main()
            pd.testing.assert_frame_equal(pd.read_pickle(path), repaired)
            backups = list(Path(tmp).glob("*.bak-*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), original)

    def test_replaces_stale_labels_and_reports_denied_objects_without_dropping_rows(
        self,
    ):
        frame = pd.DataFrame(
            {
                "key": ["SDAX-1", "SDIPR-1"],
                "main_category_id": ["1", "1"],
                "sub_category_id": ["2", ""],
                "Hauptkategorie": ["sandbox", "sandbox"],
                "Unterkategorie": ["old", "old"],
                "summary": ["keep", "keep"],
                "has_exax_clone": [True, False],
            }
        )
        seen = []

        def fetch(cloud, workspace, oid):
            seen.append((cloud, workspace, oid))
            if oid == "2":
                raise PermissionError("denied")
            return {"id": oid, "label": "Normal category"}

        result, report = asset_migration.migrate_categories(frame, fetch=fetch)
        self.assertEqual(
            result["Hauptkategorie"].tolist(), ["Normal category", "Normal category"]
        )
        self.assertEqual(result["Unterkategorie"].tolist(), ["Unbekannt", "Unbekannt"])
        self.assertEqual(result["summary"].tolist(), ["keep", "keep"])
        self.assertEqual(result["has_exax_clone"].tolist(), [True, False])
        self.assertEqual(len(seen), 2)
        self.assertTrue(
            all(w == "9926cb30-3f07-4fb2-9c83-aa4fc551c721" for _, w, _ in seen)
        )
        self.assertEqual(report["failed_objects"], 1)
        self.assertNotIn("category_asset_errors", result.columns)
        self.assertEqual(len(frame.columns), 7)

    def test_wrong_returned_identity_is_not_used(self):
        frame = pd.DataFrame(
            {"key": ["SDAX-1"], "main_category_id": ["1"], "sub_category_id": [""]}
        )
        result, report = asset_migration.migrate_categories(
            frame, fetch=lambda *a: {"id": "other", "label": "Wrong"}
        )
        self.assertEqual(result.iloc[0]["Hauptkategorie"], "Unbekannt")
        self.assertEqual(report["failed_objects"], 1)


if __name__ == "__main__":
    unittest.main()
