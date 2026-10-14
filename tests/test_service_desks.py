import itertools
import unittest
from datetime import UTC, datetime

import pandas as pd

import service_desks as desks


class DeskTests(unittest.TestCase):
    def test_every_company_subset_filters_without_cross_company_rows(self):
        frame = pd.DataFrame(
            {
                "firma": ["IPRO", "Amparex", "Euronet"],
                "key": ["SDIPR-1", "SDAX-1", "SDEU-1"],
            }
        )
        labels = ["Ipro", "Amparex", "Euronet"]
        keys = {"Ipro": "SDIPR-1", "Amparex": "SDAX-1", "Euronet": "SDEU-1"}
        for n in range(4):
            for selection in itertools.combinations(labels, n):
                with self.subTest(selection=selection):
                    self.assertEqual(
                        desks.filter_companies(frame, selection)["key"].tolist(),
                        [keys[x] for x in selection],
                    )

    def test_all_desks_use_requested_start_without_a_minimum_date(self):
        start = datetime(2025, 9, 1, tzinfo=UTC)
        for project in ("SDIPR", "SDAX", "SDEU"):
            self.assertEqual(desks.effective_start(project, start), start)
            self.assertIsNone(desks.effective_start(project, None))
        later = datetime(2026, 9, 3, tzinfo=UTC)
        self.assertEqual(desks.effective_start("SDEU", later), later)

    def test_import_keeps_older_euronet_tickets_inside_requested_window(self):
        issues = [
            {"key": "SDEU-0", "fields": {"created": "2024-12-31T23:59:59+00:00"}},
            {"key": "SDEU-1", "fields": {"created": "2026-08-31T21:59:59+00:00"}},
            {"key": "SDEU-2", "fields": {"created": "2026-08-31T22:00:00+00:00"}},
            {"key": "SDAX-3", "fields": {"created": "2026-09-02T00:00:00+00:00"}},
            {"key": "SDEU-4", "fields": {"created": "2026-09-11T00:00:00+00:00"}},
        ]
        result = desks.filter_issue_window(
            "SDEU",
            issues,
            datetime(2025, 1, 1, tzinfo=UTC),
            datetime(2026, 9, 10, tzinfo=UTC),
        )
        self.assertEqual([i["key"] for i in result], ["SDEU-1", "SDEU-2"])


if __name__ == "__main__":
    unittest.main()
