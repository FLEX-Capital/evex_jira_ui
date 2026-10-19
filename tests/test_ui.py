import unittest
from datetime import date
from unittest.mock import patch

import pandas as pd
from streamlit.testing.v1 import AppTest
from ui_env import without_ui_password


class CompanyUITests(unittest.TestCase):
    def setUp(self):
        without_ui_password(self)

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
    def setUp(self):
        without_ui_password(self)

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
            patch("asset_country.save_cache", side_effect=lambda cache, **kw: cache),
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
            patch("asset_country.save_cache", side_effect=lambda cache, **kw: cache),
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


class AuthenticationUITests(unittest.TestCase):
    def setUp(self):
        without_ui_password(self)

    def test_unicode_passwords_can_be_rejected_and_accepted_without_crashing(self):
        import os

        for password in ("ascii-password", "pässwört"):
            with (
                self.subTest(password=password),
                patch.dict(os.environ, {"UI_PASSWORD": password}),
                patch("data_loading.load_data", return_value=pd.DataFrame()),
            ):
                app = AppTest.from_file("app.py", default_timeout=30).run()
                app.sidebar.text_input[0].set_value("wrong-ä").run()
                self.assertFalse(app.exception)
                self.assertTrue(app.sidebar.error)
                app.sidebar.text_input[0].set_value(password).run()
                self.assertFalse(app.exception)
                self.assertEqual(len(app.sidebar.multiselect), 1)

    def test_password_blocks_data_and_controls_until_login_and_after_logout(self):
        import os

        with (
            patch.dict(os.environ, {"UI_PASSWORD": "test-password"}),
            patch("data_loading.load_data", return_value=pd.DataFrame()) as load,
        ):
            app = AppTest.from_file("app.py", default_timeout=30).run()
            self.assertFalse(app.exception)
            load.assert_not_called()
            self.assertEqual(len(app.sidebar.text_input), 1)
            self.assertFalse(app.sidebar.multiselect)
            app.sidebar.text_input[0].set_value("wrong").run()
            load.assert_not_called()
            app.sidebar.text_input[0].set_value("test-password").run()
            self.assertFalse(app.exception)
            self.assertEqual(len(app.sidebar.multiselect), 1)
            self.assertTrue(load.called)
            load.reset_mock()
            next(b for b in app.sidebar.button if "Logout" in b.label).click().run()
            load.assert_not_called()
            self.assertEqual(len(app.sidebar.text_input), 1)

    def test_password_only_in_env_file_gates_a_fresh_process(self):
        import tempfile
        from pathlib import Path

        # Nothing has loaded .env into the environment when the gate first runs.
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text("UI_PASSWORD=from-dotenv\n")
            with (
                patch("ui_auth.ENV_FILE", env_file),
                patch("data_loading.load_data", return_value=pd.DataFrame()) as load,
            ):
                app = AppTest.from_file("app.py", default_timeout=30).run()
                self.assertFalse(app.exception)
                load.assert_not_called()
                self.assertEqual(len(app.sidebar.text_input), 1)
                app.sidebar.text_input[0].set_value("from-dotenv").run()
                self.assertEqual(len(app.sidebar.multiselect), 1)


class StaleCountryRefreshUITests(unittest.TestCase):
    def setUp(self):
        without_ui_password(self)

    _frame = CountryRefreshUITests._frame

    def test_stale_ticket_snapshot_keeps_country_mappings_but_not_tickets(self):
        frame = self._frame()
        resolved = {"asset-1": "Deutschland"}
        with (
            patch("data_loading.load_data", return_value=frame),
            patch("asset_country.load_cache", return_value={}),
            patch("asset_country.save_cache") as save,
            patch("data_loading.save_data") as save_tickets,
            patch("interactive.render_interactive"),
        ):
            app = AppTest.from_file("app.py", default_timeout=30).run()
            app.sidebar.date_input[0].set_value(
                (date(2026, 9, 1), date(2026, 9, 10))
            ).run()
            observed = {"revision": "before"}

            def concurrent_country_refresh(*args, **kwargs):
                observed["revision"] = "after"
                return frame, resolved, {}

            with (
                patch(
                    "data_loading.cache_revision",
                    side_effect=lambda *a, **k: observed["revision"],
                ),
                patch(
                    "asset_country.refresh_countries",
                    side_effect=concurrent_country_refresh,
                ),
            ):
                next(b for b in app.button if "Länder" in b.label).click().run()
            self.assertFalse(app.exception)
            self.assertTrue(any("parallel" in e.value for e in app.error))
            # Resolved lookups are kept so a retry needs no new Assets calls.
            save.assert_called_once_with(resolved, baseline={})
            save_tickets.assert_not_called()


if __name__ == "__main__":
    unittest.main()
