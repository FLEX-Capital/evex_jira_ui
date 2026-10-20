import importlib
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd

import jira_loader
from issue_links import IssueLink, LinkGraph


def issue(
    key="SDEU-1",
    *,
    main_id="main-1",
    sub_id="sub-1",
    request_status="Fertig",
    links=None,
    **top_level,
):
    raw = {
        "key": key,
        "fields": {
            "summary": "Printer cannot connect",
            "description": "Diagnostic text",
            "status": {"name": "Done", "statusCategory": {"name": "Done"}},
            "issuetype": {"name": "Service request"},
            "created": "2026-09-01T08:00:00.000+0000",
            "updated": "2026-09-01T12:00:00.000+0000",
            "labels": ["branch"],
            "priority": {"name": "High"},
            "customfield_10065": ["Support"],
            "customfield_10010": {
                "requestType": {"name": "Incident"},
                "currentStatus": {
                    "status": request_status,
                    "statusDate": {"jira": "2026-09-01T12:00:00.000+0000"},
                },
                "_links": {"agent": "https://jira.example/SDEU-1"},
            },
            "comment": {"comments": [{"body": "Investigating"}]},
            "customfield_10675": {"value": "Portal"},
            "customfield_10680": [] if main_id is None else [{"objectId": main_id}],
            "customfield_10679": [] if sub_id is None else [{"objectId": sub_id}],
            "customfield_10673": [{"objectId": "hq-1"}],
            "customfield_10674": [{"objectId": "branch-1"}],
            "issuelinks": links or [],
        },
    }
    for field in ("customfield_10680", "customfield_10679"):
        for reference in raw["fields"][field]:
            reference["workspaceId"] = jira_loader.WORKSPACE_ID
    raw.update(top_level)
    return raw


class TransformationTests(unittest.TestCase):
    def test_rejected_category_reference_cannot_borrow_same_id_label(self):
        for rejected_field in ("customfield_10680", "customfield_10679"):
            for workspace in ("sandbox", None):
                with self.subTest(field=rejected_field, workspace=workspace):
                    raw = issue(main_id="41", sub_id="41")
                    for field in ("customfield_10680", "customfield_10679"):
                        raw["fields"][field][0]["workspaceId"] = (
                            workspace if field == rejected_field else jira_loader.WORKSPACE_ID
                        )
                    with patch.object(
                        jira_loader,
                        "fetch_asset_object",
                        return_value={"id": "41", "label": "Normal category"},
                    ):
                        enriched = jira_loader.enrich_issue_assets(raw)
                    row = self.transformation.load_issues_Euronet([enriched]).iloc[0]
                    rejected_column = (
                        "Hauptkategorie"
                        if rejected_field == "customfield_10680"
                        else "Unterkategorie"
                    )
                    valid_column = (
                        "Unterkategorie"
                        if rejected_field == "customfield_10680"
                        else "Hauptkategorie"
                    )
                    self.assertEqual(row[rejected_column], "Unbekannt")
                    self.assertEqual(row[valid_column], "Normal category")

    def test_rejected_escalation_reference_cannot_borrow_same_id_country(self):
        """A foreign-workspace Ansprechpartner must not outrank a valid Filiale.

        Country resolution keys its cache by objectId alone, so a sandbox
        objectId that collides with a normal-workspace one would resolve to
        that object's country - and since Ansprechpartner is checked first, it
        would override a Filiale country that was correct.
        """
        for workspace in ("sandbox", jira_loader.WORKSPACE_ID, None):
            with self.subTest(workspace=workspace):
                raw = issue()
                reference = {"objectId": "41"}
                if workspace is not None:
                    reference["workspaceId"] = workspace
                raw["fields"]["customfield_10689"] = [reference]

                row = self.transformation.load_issues_Euronet([raw]).iloc[0]

                # A reference with no workspaceId at all is legacy raw data,
                # not a cross-workspace reference, so it is still accepted.
                expected = "" if workspace == "sandbox" else "ID_41"
                self.assertEqual(row["ansprechpartner"], expected)
                # The rejection must not take the valid links down with it.
                self.assertEqual(row["filiale"], "ID_branch-1")
                self.assertEqual(row["zentrale"], "ID_hq-1")

    def test_refresh_supersedes_migration_errors_only_for_refreshed_rows(self):
        import asset_migration

        old = self.transformation.load_issues_Euronet([issue(), issue(key="SDEU-2")])

        def denied(*args):
            raise PermissionError("denied")

        old, _ = asset_migration.migrate_categories(old, fetch=denied)
        for fails in (False, True):

            def fetch(cloud, workspace, oid, fails=fails, **kwargs):
                if fails and oid == "main-1":
                    raise PermissionError("denied")
                return {"id": oid, "label": "Recovered"}

            with self.subTest(fails=fails):
                raw = issue()
                raw["fields"]["customfield_10673"] = []
                raw["fields"]["customfield_10674"] = []
                with patch.object(jira_loader, "fetch_asset_object", side_effect=fetch):
                    enriched = jira_loader.enrich_issue_assets(raw)
                fresh = self.transformation.load_issues_Euronet([enriched])
                result = self.transformation.upsert_jira_data(old, fresh).set_index("key")
                self.assertEqual(
                    result.loc["SDEU-1", "Hauptkategorie"],
                    "Unbekannt" if fails else "Recovered",
                )
                self.assertNotIn("asset_errors", result.columns)
                self.assertNotIn("category_asset_errors", result.columns)

    @classmethod
    def setUpClass(cls):
        # Replace only the external client boundary; transformation stays real.
        sys.modules.pop("data_transformation", None)
        with patch("jira.JIRA", return_value=MagicMock()):
            cls.transformation = importlib.import_module("data_transformation")
        fixture_path = Path(__file__).parent / "fixtures" / "legacy_transform.json"
        cls.legacy_golden = json.loads(fixture_path.read_text(encoding="utf-8"))

    def test_import_does_not_connect_to_jira(self):
        """Catches cached/offline transformation imports opening a Jira connection."""
        sys.modules.pop("data_transformation", None)
        with patch("jira.JIRA") as jira_constructor:
            importlib.import_module("data_transformation")
        jira_constructor.assert_not_called()

    def test_euronet_uses_amparex_fields_metrics_and_keeps_generic_links(self):
        """Catches SDEU using new field IDs or inheriting escalation flags."""
        linked = [{"type": {"name": "Relates"}, "outwardIssue": {"key": "EXAX-7"}}]
        raw = issue(
            links=linked,
            asset_labels={"main-1": "Hardware", "sub-1": "Printer"},
            assets_workspace_id=jira_loader.WORKSPACE_ID,
            assets_cloud_id=jira_loader.CLOUD_ID,
            asset_errors=["main-2: HTTP 403"],
        )
        row = self.transformation.load_issues_Euronet(
            [raw],
            link_graph=LinkGraph({"EXAX-7": (IssueLink("Cloners", "outward", "AX-9"),)}, ()),
        ).iloc[0]

        self.assertEqual(row["firma"], "Euronet")
        self.assertEqual(row["request_type"], "Incident")
        self.assertEqual(row["source"], "Portal")
        self.assertEqual(row["Hauptkategorie"], "Hardware")
        self.assertEqual(row["Unterkategorie"], "Printer")
        self.assertEqual(row["clones"], "EXAX-7")
        self.assertEqual(row["clone_types"], "Relates")
        self.assertEqual(row["clones_of_clones"], "AX-9")
        self.assertTrue(bool(row["is_done"]))
        self.assertAlmostEqual(row["time_to_resolution_h"], 4.0)
        self.assertFalse(bool(row["has_exax_clone"]))
        self.assertFalse(bool(row["has_axt_clone_clone"]))
        for column in ("assets_workspace_id", "assets_cloud_id", "asset_errors"):
            self.assertNotIn(column, row.index)

    def test_normal_assets_marker_blocks_stale_static_fallback(self):
        """Catches unresolved normal IDs being mislabeled by the legacy map."""
        raw = issue(
            asset_labels={},
            assets_workspace_id=jira_loader.WORKSPACE_ID,
            assets_cloud_id=jira_loader.CLOUD_ID,
        )
        with patch.dict(self.transformation.object_id_to_name, {"main-1": "Stale sandbox label"}):
            row = self.transformation.load_issues_Euronet([raw]).iloc[0]
        self.assertEqual(row["Hauptkategorie"], "Unbekannt")
        self.assertEqual(row["Unterkategorie"], "Unbekannt")

    def test_legacy_issue_uses_static_category_map(self):
        """Catches removal of the fallback for unenriched cached/raw issues."""
        with patch.dict(
            self.transformation.object_id_to_name,
            {"main-1": "Legacy main", "sub-1": "Legacy sub"},
        ):
            row = self.transformation.load_issues_Amparex([issue(key="SDAX-1")]).iloc[0]
        self.assertEqual(row["firma"], "Amparex")
        self.assertEqual(row["Hauptkategorie"], "Legacy main")
        self.assertEqual(row["Unterkategorie"], "Legacy sub")

    def test_cloned_by_keeps_first_link_semantics(self):
        """Catches later inward links changing the legacy first-link report."""
        links = [
            {"type": {"name": "Relates"}, "outwardIssue": {"key": "OTHER-1"}},
            {"type": {"name": "Cloners"}, "inwardIssue": {"key": "SOURCE-1"}},
        ]
        row = self.transformation.load_issues_Amparex(
            [issue(key="SDAX-1", links=links)],
            link_graph=LinkGraph({"OTHER-1": ()}, ()),
        ).iloc[0]
        self.assertEqual(row["cloned_by"], "")

    def test_old_desk_complete_fixtures_match_original_non_assets_behavior(self):
        """Catches shared-loader refactors changing established desk output."""
        links = [
            {
                "type": {"name": "Cloners"},
                "outwardIssue": {"key": "EXAX-7"},
                "inwardIssue": {"key": "SOURCE-1"},
            }
        ]
        cases = (
            ("load_issues", issue(key="SDIPR-1", links=links)),
            ("load_issues_Amparex", issue(key="SDAX-1", links=links)),
        )
        for loader_name, raw in cases:
            with self.subTest(loader=loader_name):
                actual = getattr(self.transformation, loader_name)(
                    [raw],
                    link_graph=LinkGraph(
                        {"EXAX-7": (IssueLink("Cloners", "outward", "AX-9"),)}, ()
                    ),
                )
            columns = self.legacy_golden["columns"]
            expected_row = self.legacy_golden["rows"][loader_name]
            expected_dtypes = self.legacy_golden["dtypes"]
            self.assertEqual(list(actual[columns].columns), columns)
            for column in columns:
                with self.subTest(loader=loader_name, column=column):
                    value = actual.iloc[0][column]
                    if isinstance(value, pd.Timestamp):
                        value = value.isoformat()
                    elif value is pd.NA or value is pd.NaT:
                        value = None
                    elif isinstance(value, np.generic):
                        value = value.item()
                    self.assertEqual(value, expected_row[column])
                    self.assertEqual(str(actual[column].dtype), expected_dtypes[column])

    def test_optional_nulls_and_empty_input_return_compatible_full_schema(self):
        """Catches optional fields crashing loaders or empty refreshes losing schema."""
        raw = issue(main_id=None, sub_id=None)
        raw["fields"].update(
            {
                "priority": None,
                "comment": None,
                "customfield_10010": None,
                "customfield_10065": None,
                "customfield_10675": None,
                "labels": None,
            }
        )
        row = self.transformation.load_issues_Euronet([raw]).iloc[0]
        empty = self.transformation.load_issues_Euronet([])

        self.assertEqual(row["priority"], "")
        self.assertEqual(row["comments"], "")
        self.assertEqual(row["request_type"], "")
        self.assertEqual(row["currentstatus_name"], "")
        self.assertEqual(row["category"], [])
        self.assertEqual(row["Hauptkategorie"], "Unbekannt")
        self.assertEqual(row["Unterkategorie"], "Unbekannt")
        self.assertTrue(empty.empty)
        self.assertEqual(
            list(empty.columns),
            list(self.transformation.load_issues_Amparex([]).columns),
        )
        self.assertTrue(str(empty["created"].dtype).startswith("datetime64[ns, Europe/Berlin]"))
        self.assertEqual(str(empty["is_done"].dtype), "boolean")
        self.assertEqual(empty["has_exax_clone"].dtype, bool)
        self.assertEqual(empty["has_axt_clone_clone"].dtype, bool)

    def test_project_dispatch_selects_the_configured_loader(self):
        """Catches a supported project being routed through the wrong company."""
        for project, expected_firma in (
            ("SDIPR", "IPRO"),
            ("SDAX", "Amparex"),
            ("SDEU", "Euronet"),
        ):
            with self.subTest(project=project):
                row = self.transformation.load_project_issues(
                    project, [issue(key=f"{project}-1")]
                ).iloc[0]
                self.assertEqual(row["firma"], expected_firma)

    def test_project_dispatch_rejects_unknown_project(self):
        """Catches silent misclassification of an unsupported project."""
        with self.assertRaisesRegex(ValueError, "UNKNOWN"):
            self.transformation.load_project_issues("UNKNOWN", [])

    def test_upsert_handles_empty_cache_duplicates_schema_and_explicit_nulls(self):
        """Catches duplicates, stale refreshed values, and unrelated-column loss."""
        old = pd.DataFrame(
            [
                {
                    "key": "A-1",
                    "summary": "older duplicate",
                    "priority": "Low",
                    "local_note": "discard",
                    "is_done": False,
                },
                {
                    "key": "A-1",
                    "summary": "old",
                    "priority": "High",
                    "local_note": "keep",
                    "is_done": False,
                },
            ]
        )
        new = pd.DataFrame(
            [
                {
                    "key": "A-1",
                    "summary": "intermediate",
                    "priority": "Medium",
                    "is_done": True,
                },
                {"key": "A-1", "summary": "new", "priority": None, "is_done": True},
                {
                    "key": "A-2",
                    "summary": "inserted",
                    "priority": "Low",
                    "is_done": False,
                },
            ]
        )
        result = self.transformation.upsert_jira_data(old, new).set_index("key")
        bootstrapped = self.transformation.upsert_jira_data(pd.DataFrame(), new)

        self.assertTrue(result.index.is_unique)
        self.assertEqual(result.loc["A-1", "summary"], "new")
        self.assertTrue(pd.isna(result.loc["A-1", "priority"]))
        self.assertEqual(result.loc["A-1", "local_note"], "keep")
        self.assertEqual(str(result["is_done"].dtype), "boolean")
        self.assertTrue(result["is_done"].isna().all())
        self.assertEqual(set(bootstrapped["key"]), {"A-1", "A-2"})
        self.assertTrue(bootstrapped["key"].is_unique)


class CalendarDayResolutionTests(unittest.TestCase):
    def test_calendar_days_follow_berlin_dates_across_clock_changes(self):
        from data_transformation import enrich_jira_time_metrics

        for created, resolved, days in [
            ("2026-03-29T09:00:00+02:00", "2026-03-30T09:00:00+02:00", 1),
            ("2026-10-25T09:00:00+01:00", "2026-10-26T09:00:00+01:00", 1),
            ("2026-03-29T00:30:00+01:00", "2026-03-29T23:30:00+02:00", 0),
        ]:
            with self.subTest(created=created):
                frame = pd.DataFrame(
                    [
                        dict(
                            created=created,
                            updated=resolved,
                            currentstatus_date=resolved,
                            currentstatus_name="Fertig",
                        )
                    ]
                )
                result = enrich_jira_time_metrics(frame)
                self.assertEqual(result.loc[0, "time_to_resolution_calendar_days"], days)
                self.assertEqual(
                    result.loc[0, "resolution"],
                    "Same day" if days == 0 else "> 1 day",
                )


if __name__ == "__main__":
    unittest.main()
