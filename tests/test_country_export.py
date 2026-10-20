import unittest
from unittest.mock import patch

import jira_country_export as exporter


class CountryExportTests(unittest.TestCase):
    def test_foreign_workspace_does_not_abort_other_tickets_or_use_foreign_id(self):
        issues = [
            {
                "key": "SDEU-1",
                "fields": {
                    "customfield_10673": [{"workspaceId": "sandbox", "objectId": "1"}]
                },
            },
            {
                "key": "SDAX-2",
                "fields": {
                    "customfield_10673": [
                        {"workspaceId": exporter.WORKSPACE_ID, "objectId": "2"}
                    ]
                },
            },
        ]
        with patch.object(
            exporter,
            "_fetch_asset_with_retry",
            return_value={
                "id": "2",
                "attributes": [
                    {
                        "objectTypeAttribute": {"name": "Land"},
                        "objectAttributeValues": [{"displayValue": "DE"}],
                    }
                ],
            },
        ) as resolve:
            result = exporter.resolve_countries(issues)
        self.assertEqual(result, {"SDEU-1": None, "SDAX-2": "DE"})
        self.assertTrue(all(call.args[0] == "2" for call in resolve.call_args_list))

    def test_fetch_uses_normal_cloud_workspace_and_verifies_identity(self):
        with (
            patch.object(
                exporter, "fetch_asset_object", return_value={"id": "other"}
            ) as fetch,
            self.assertRaises(ValueError),
        ):
            exporter._fetch_asset_with_retry("2")
        self.assertEqual(
            fetch.call_args.args,
            (
                "242cf880-c51a-4277-9381-781d5ae181df",
                "9926cb30-3f07-4fb2-9c83-aa4fc551c721",
                "2",
            ),
        )


if __name__ == "__main__":
    unittest.main()
