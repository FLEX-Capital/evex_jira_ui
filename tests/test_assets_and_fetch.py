import importlib
import json
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


class IncrementalFlushTests(unittest.TestCase):
    """A multi-hour fetch must not lose everything when it is interrupted."""

    def setUp(self):
        self.enterContext(
            patch.multiple(
                loader,
                JIRA_URL="https://jira.example",
                JIRA_USERNAME="test-user",
                JIRA_PASSWORD=token_hex(16),
            )
        )

    def _fake_jira(self, pages):
        fake = MagicMock()
        fake.myself.return_value = {"timeZone": "UTC"}
        fake.project.return_value = object()
        fake.enhanced_search_issues.side_effect = pages
        return fake

    @staticmethod
    def _page(keys, token):
        return {
            "issues": [
                {"key": f"SDAX-{k}", "fields": {"created": "2026-08-01T00:00:00+00:00"}}
                for k in keys
            ],
            "nextPageToken": token,
        }

    def test_partial_callback_receives_only_new_issues_each_flush(self):
        pages = [
            self._page(range(60), "t1"),
            self._page(range(60, 120), "t2"),
            self._page(range(120, 150), None),
        ]
        flushes = []
        with (
            patch.object(loader, "JIRA", return_value=self._fake_jira(pages)),
            patch.object(loader, "enrich_issue_assets", side_effect=lambda i: i),
        ):
            issues = loader.fetch_jira_issues(
                datetime(2025, 1, 1, tzinfo=UTC),
                datetime(2026, 9, 10, tzinfo=UTC),
                project="SDAX",
                save_path=None,
                partial_cb=lambda batch: flushes.append([i["key"] for i in batch]),
                partial_every=100,
            )
        # 120 accumulated triggers the first flush; the tail flushes at the end.
        self.assertEqual([len(f) for f in flushes], [120, 30])
        # Crucially disjoint: the callback appends, so overlap would duplicate.
        flushed = [key for batch in flushes for key in batch]
        self.assertEqual(len(flushed), len(set(flushed)))
        self.assertEqual(len(issues), 150)

    def test_tail_is_flushed_even_below_the_threshold(self):
        flushes = []
        with (
            patch.object(
                loader,
                "JIRA",
                return_value=self._fake_jira([self._page(range(3), None)]),
            ),
            patch.object(loader, "enrich_issue_assets", side_effect=lambda i: i),
        ):
            loader.fetch_jira_issues(
                datetime(2025, 1, 1, tzinfo=UTC),
                datetime(2026, 9, 10, tzinfo=UTC),
                project="SDAX",
                save_path=None,
                partial_cb=flushes.append,
                partial_every=100,
            )
        self.assertEqual([len(f) for f in flushes], [3])

    def test_no_callback_keeps_the_previous_behaviour(self):
        with (
            patch.object(
                loader,
                "JIRA",
                return_value=self._fake_jira([self._page(range(5), None)]),
            ),
            patch.object(loader, "enrich_issue_assets", side_effect=lambda i: i),
        ):
            issues = loader.fetch_jira_issues(
                datetime(2025, 1, 1, tzinfo=UTC),
                datetime(2026, 9, 10, tzinfo=UTC),
                project="SDAX",
                save_path=None,
            )
        self.assertEqual(len(issues), 5)


class EnrichmentFootprintTests(unittest.TestCase):
    """The enriched issue is held in memory for every ticket in a backfill."""

    def test_country_is_kept_but_the_asset_object_is_not(self):
        issue = {
            "key": "SDAX-1",
            "fields": {
                "customfield_10673": [
                    {"objectId": "7", "workspaceId": loader.WORKSPACE_ID}
                ]
            },
        }
        asset = {
            "id": "7",
            "label": "Acme",
            # Stands in for the ~24 KB of attributes a real Assets object carries.
            "attributes": [
                {
                    "objectTypeAttribute": {"name": "Land"},
                    "objectAttributeValues": [{"displayValue": "DE"}],
                },
                {
                    "objectTypeAttribute": {"name": "Ballast"},
                    "objectAttributeValues": [{"displayValue": "x" * 5000}],
                },
            ],
        }
        with patch.object(loader, "fetch_asset_object", return_value=asset):
            enriched = loader.enrich_issue_assets(issue)
        self.assertEqual(enriched["customer_country"], "DE")
        self.assertNotIn("customer", enriched)
        self.assertNotIn("Ballast", json.dumps(enriched))


class TrimTests(unittest.TestCase):
    """Trimming must be invisible to the transformation, only to memory."""

    def _full_issue(self):
        fields = {name: f"value-{name}" for name in loader.RETAINED_FIELDS}
        fields.update(
            {
                "created": "2026-08-01T00:00:00.000+0000",
                "updated": "2026-08-02T00:00:00.000+0000",
                "labels": ["a"],
                "issuelinks": [],
                "comment": {"comments": [{"body": "hallo"}]},
                "status": {"name": "Fertig", "statusCategory": {"name": "Done"}},
                "issuetype": {"name": "Allgemeine Anfrage"},
                "priority": {"name": "Medium"},
                "customfield_10065": [],
                "customfield_10675": {"value": "Anruf"},
                "customfield_10010": {},
            }
        )
        # The ~150 fields a real search response also carries.
        for n in range(150):
            fields[f"customfield_9{n:03d}"] = {"junk": "x" * 200}
        fields["creator"] = {"displayName": "x" * 500}
        return {"key": "SDAX-1", "fields": fields}

    def test_trim_drops_junk_and_keeps_every_retained_field(self):
        trimmed = loader.trim_issue(self._full_issue())
        self.assertEqual(set(trimmed["fields"]), set(loader.RETAINED_FIELDS))
        self.assertNotIn("creator", trimmed["fields"])
        self.assertEqual(trimmed["key"], "SDAX-1")

    def test_trim_preserves_top_level_enrichment_keys(self):
        issue = self._full_issue()
        issue["customer_country"] = "DE"
        issue["asset_labels"] = {"7": "Acme"}
        trimmed = loader.trim_issue(issue)
        self.assertEqual(trimmed["customer_country"], "DE")
        self.assertEqual(trimmed["asset_labels"], {"7": "Acme"})

    def test_extraction_is_identical_before_and_after_trimming(self):
        """The invariant that matters: trimming changes size, never output."""
        import data_transformation as transform

        issue = self._full_issue()
        self.assertEqual(
            transform._extract_issue(issue, "\n"),
            transform._extract_issue(loader.trim_issue(issue), "\n"),
        )

    def test_dedupe_still_sees_the_updated_timestamp(self):
        trimmed = loader.trim_issue(self._full_issue())
        self.assertEqual(
            loader._issue_updated_ts(trimmed), "2026-08-02T00:00:00.000+0000"
        )
