import unittest
from datetime import date
from unittest.mock import patch

import pandas as pd
from streamlit.testing.v1 import AppTest


class CompanyUITests(unittest.TestCase):
    def test_company_selector_defaults_all_and_empty_selection_stops_cleanly(self):
        with patch("data_loading.load_data", return_value=pd.DataFrame()):
            app = AppTest.from_file("app.py", default_timeout=30).run()
            self.assertEqual(len(app.sidebar.multiselect), 1)
            self.assertEqual(
                app.sidebar.multiselect[0].value, ["Ipro", "Amparex", "Euronet"]
            )
            app.sidebar.multiselect[0].set_value([]).run()
            self.assertFalse(app.exception)
            self.assertTrue(any("Firma" in info.value for info in app.info))
            self.assertEqual(
                app.multiselect(key="temporary_history_desks").value,
                ["SDIPR", "SDAX", "SDEU"],
            )

    def test_selected_companies_reach_raw_data_and_empty_optional_charts_render(self):
        from datetime import date

        from test_transformation import issue

        from data_transformation import load_project_issues

        frames = []
        for project in ["SDIPR", "SDAX", "SDEU"]:
            raw = issue(key=project + "-1", main_id=None, sub_id=None)
            raw["fields"]["customfield_10010"] = None
            raw["fields"]["customfield_10675"] = None
            frames.append(load_project_issues(project, [raw]))
        frame = pd.concat(frames, ignore_index=True)
        with (
            patch("data_loading.load_data", return_value=frame),
            patch("interactive.render_interactive"),
        ):
            app = AppTest.from_file("app.py", default_timeout=30).run()
            app.sidebar.date_input[0].set_value(
                (date(2026, 9, 1), date(2026, 9, 10))
            ).run()
            self.assertFalse(app.exception)
            raw_frame = app.dataframe[0].value
            self.assertEqual(set(raw_frame["key"]), {"SDIPR-1", "SDAX-1", "SDEU-1"})
            app.sidebar.multiselect[0].set_value(["Euronet", "Ipro"]).run()
            self.assertFalse(app.exception)
            self.assertEqual(set(app.dataframe[0].value["key"]), {"SDIPR-1", "SDEU-1"})


class CountryRefreshUITests(unittest.TestCase):
    """The Länder button rewrites the whole cache, not the filtered view."""

    def _frame(self):
        from test_transformation import issue

        from data_transformation import load_project_issues

        frame = load_project_issues("SDEU", [issue(key="SDEU-1")])
        # A second ticket outside the dashboard's date window: the button must
        # still update it, or countries silently depend on the current filter.
        old = load_project_issues("SDEU", [issue(key="SDEU-2")])
        old["created"] = pd.Timestamp("2020-01-01", tz="Europe/Berlin")
        return pd.concat([frame, old], ignore_index=True)

    def test_button_resolves_countries_for_tickets_outside_the_date_window(self):
        frame = self._frame()
        saved = {}

        with (
            patch("data_loading.load_data", return_value=frame),
            patch("data_loading.save_data", side_effect=lambda d: saved.update(df=d)),
            patch("asset_country.load_cache", return_value={}),
            patch("asset_country.save_cache"),
            patch(
                "jira_country_export.resolve_asset_country_strict",
                lambda object_id: "Österreich",
            ),
            patch("interactive.render_interactive"),
        ):
            app = AppTest.from_file("app.py", default_timeout=60).run()
            # The app st.stop()s when the window is empty, so the tab (and its
            # buttons) never render. Put SDEU-1 inside it; SDEU-2 stays out.
            app.sidebar.date_input[0].set_value(
                (date(2026, 9, 1), date(2026, 9, 10))
            ).run()
            buttons = [b for b in app.button if "Länder" in b.label]
            self.assertEqual(len(buttons), 1, "Länder button missing")
            buttons[0].click().run()
            self.assertFalse(app.exception)

        self.assertIn("df", saved)
        # Both rows written, including the one the date filter hides.
        self.assertEqual(set(saved["df"]["key"]), {"SDEU-1", "SDEU-2"})
        self.assertEqual(set(saved["df"]["Land"]), {"Österreich"})

    def test_failed_lookup_is_reported_and_not_cached(self):
        frame = self._frame()
        cache = {}

        def boom(object_id):
            raise RuntimeError("403")

        with (
            patch("data_loading.load_data", return_value=frame),
            patch("data_loading.save_data"),
            patch("asset_country.load_cache", return_value=cache),
            patch("asset_country.save_cache"),
            patch("jira_country_export.resolve_asset_country_strict", boom),
            patch("interactive.render_interactive"),
        ):
            app = AppTest.from_file("app.py", default_timeout=60).run()
            # The app st.stop()s when the window is empty, so the tab (and its
            # buttons) never render. Put SDEU-1 inside it; SDEU-2 stays out.
            app.sidebar.date_input[0].set_value(
                (date(2026, 9, 1), date(2026, 9, 10))
            ).run()
            buttons = [b for b in app.button if "Länder" in b.label]
            buttons[0].click().run()
            self.assertFalse(app.exception)
            warnings = " ".join(w.value for w in app.warning)
            self.assertIn("fehlgeschlagen", warnings)

        self.assertEqual(cache, {}, "a failed lookup must not be cached")


if __name__ == "__main__":
    unittest.main()
