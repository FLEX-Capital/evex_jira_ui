import itertools
import unittest
from datetime import UTC, date, datetime

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

    def test_berlin_day_window_spans_whole_days_across_dst(self):
        # 2026-03-29 has 23 hours in Berlin.
        start = desks.berlin_day_start(date(2026, 3, 29))
        end = desks.berlin_day_end(date(2026, 3, 29))
        self.assertEqual(desks.as_utc(start).isoformat(), "2026-03-28T23:00:00+00:00")
        self.assertEqual(
            desks.as_utc(end).isoformat(), "2026-03-29T21:59:59.999999+00:00"
        )

    def test_selected_projects_dedupes_and_rejects_unknown_or_empty(self):
        self.assertEqual(desks.selected_projects(None), ("SDIPR", "SDAX", "SDEU"))
        self.assertEqual(
            desks.selected_projects(["SDEU", "SDAX", "SDEU"]), ("SDEU", "SDAX")
        )
        for invalid in ([], ["UNKNOWN"]):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                desks.selected_projects(invalid)


if __name__ == "__main__":
    unittest.main()
