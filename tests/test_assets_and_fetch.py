import importlib
import unittest
from datetime import UTC, datetime
from secrets import token_hex
from unittest.mock import MagicMock, patch

with patch("jira.JIRA"):
    loader = importlib.import_module("jira_loader")


class AssetsTests(unittest.TestCase):
    def setUp(self):
        loader.fetch_asset_object.cache_clear()

    def test_enrichment_uses_normal_references_and_discards_old_labels(self):
        issue = {
            "key": "SDEU-1",
            "fields": {
                "customfield_10680": [
                    {"workspaceId": loader.WORKSPACE_ID, "objectId": "41"}
                ]
            },
            "asset_labels": {"41": "Sandbox"},
            "customer": {"id": "old"},
            "customer_country": "old",
        }
        with patch.object(
            loader,
            "fetch_asset_object",
            return_value={"id": "41", "label": "Normal category"},
        ) as fetch:
            enriched = loader.enrich_issue_assets(issue)
        self.assertEqual(enriched["asset_labels"], {"41": "Normal category"})
        self.assertEqual(
            enriched["assets_workspace_id"], "9926cb30-3f07-4fb2-9c83-aa4fc551c721"
        )
        self.assertNotIn("customer", enriched)
        self.assertIsNone(enriched["customer_country"])
        self.assertEqual(
            fetch.call_args.args, (loader.CLOUD_ID, loader.WORKSPACE_ID, "41")
        )

    def test_workspace_mismatch_never_reinterprets_object_id(self):
        issue = {
            "key": "SDEU-1",
            "fields": {
                "customfield_10673": [{"workspaceId": "sandbox", "objectId": "41"}]
            },
        }
        with patch.object(loader, "fetch_asset_object") as fetch:
            enriched = loader.enrich_issue_assets(issue)
            self.assertIn("workspace", enriched["asset_errors"][0])
            self.assertEqual(enriched["asset_labels"], {})
            fetch.assert_not_called()

    def test_permission_error_surfaces_without_sandbox_fallback(self):
        issue = {
            "key": "SDEU-1",
            "fields": {
                "customfield_10673": [
                    {"workspaceId": loader.WORKSPACE_ID, "objectId": "41"}
                ]
            },
        }
        with patch.object(
            loader, "fetch_asset_object", side_effect=PermissionError("forbidden")
        ):
            enriched = loader.enrich_issue_assets(issue)
            self.assertEqual(enriched["asset_labels"], {})
            self.assertTrue(enriched["asset_errors"])

    def test_schema_discovery_uses_normal_cloud_and_workspace(self):
        with patch.object(
            loader, "jira_request", return_value={"values": []}
        ) as request:
            loader.fetch_object_schema_list()
        url = request.call_args.args[0]
        self.assertIn("/242cf880-c51a-4277-9381-781d5ae181df/", url)
        self.assertIn("/9926cb30-3f07-4fb2-9c83-aa4fc551c721/", url)


class FetchTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(
            patch.multiple(
                loader,
                JIRA_URL="https://jira.example",
                JIRA_USERNAME="test-user",
                JIRA_PASSWORD=token_hex(16),
            )
        )

    def test_search_uses_account_timezone_and_requested_window_for_euronet(self):
        fake = MagicMock()
        fake.myself.return_value = {"timeZone": "America/New_York"}
        fake.project.return_value = object()
        fake.enhanced_search_issues.return_value = {
            "issues": [
                {"key": "SDEU-0", "fields": {"created": "2024-12-31T23:59:59+00:00"}},
                {"key": "SDEU-3", "fields": {"created": "2025-01-01T00:00:00+00:00"}},
                {"key": "SDEU-1", "fields": {"created": "2026-08-31T21:59:59+00:00"}},
                {"key": "SDEU-2", "fields": {"created": "2026-08-31T22:00:00+00:00"}},
            ]
        }
        with (
            patch.object(loader, "JIRA", return_value=fake),
            patch.object(loader, "enrich_issue_assets", side_effect=lambda i: i),
        ):
            issues = loader.fetch_jira_issues(
                datetime(2025, 1, 1, tzinfo=UTC),
                datetime(2026, 9, 10, tzinfo=UTC),
                project="SDEU",
                save_path=None,
            )
        self.assertEqual([i["key"] for i in issues], ["SDEU-3", "SDEU-1", "SDEU-2"])
        self.assertIn(
            "created >= '2024-12-31 19:00'",
            fake.enhanced_search_issues.call_args.kwargs["jql_str"],
        )

    def test_reversed_window_does_not_connect(self):
        with patch.object(loader, "JIRA") as connect:
            result = loader.fetch_jira_issues(
                datetime(2026, 8, 2, tzinfo=UTC),
                datetime(2026, 8, 1, tzinfo=UTC),
                project="SDEU",
                save_path=None,
            )
        self.assertEqual(result, [])
        connect.assert_not_called()

    def test_incomplete_result_cap_raises_instead_of_reporting_success(self):
        fake = MagicMock()
        fake.myself.return_value = {"timeZone": "Europe/Berlin"}
        fake.enhanced_search_issues.return_value = {
            "issues": [
                {"key": "SDAX-1", "fields": {"created": "2026-09-02T00:00:00Z"}}
            ],
            "nextPageToken": "more",
        }
        with (
            patch.object(loader, "JIRA", return_value=fake),
            patch.object(loader, "enrich_issue_assets", side_effect=lambda i: i),
            self.assertRaisesRegex(RuntimeError, "limit"),
        ):
            loader.fetch_jira_issues(
                datetime(2026, 9, 1, tzinfo=UTC),
                datetime(2026, 9, 10, tzinfo=UTC),
                max_issues=1,
                project="SDAX",
                save_path=None,
            )


if __name__ == "__main__":
    unittest.main()
