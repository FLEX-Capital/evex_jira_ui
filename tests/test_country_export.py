import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pandas as pd
import pytest

import jira_country_export as exporter
from reporting_policy import BERLIN
from settings import load_settings


@pytest.mark.parametrize("end_day", ["2026-03-29", "2026-10-25"])
def test_export_end_day_is_inclusive_across_dst(end_day: str) -> None:
    """Past local days include late night and exclude the following midnight."""
    from service_desks import filter_issue_window

    midnight = datetime.fromisoformat(end_day).replace(tzinfo=BERLIN)
    following = midnight + timedelta(days=1)
    values = [
        {
            "key": "SDAX-1",
            "fields": {"created": (following - timedelta(microseconds=1)).isoformat()},
        },
        {"key": "SDAX-2", "fields": {"created": following.isoformat()}},
    ]

    def fetch(start: datetime, end: datetime, **kwargs: Any) -> list[dict]:
        assert end == following - timedelta(microseconds=1)
        return filter_issue_window("SDAX", values, start, end)

    with patch.object(exporter, "fetch_jira_issues", side_effect=fetch):
        result = exporter.load_issues(
            "unused",
            True,
            "SDAX",
            end_day,
            end_day,
            100,
            now=lambda: datetime(2026, 12, 1, tzinfo=UTC),
        )
    assert [value["key"] for value in result] == ["SDAX-1"]


def test_export_default_today_is_capped_at_invocation() -> None:
    """The default endpoint follows Berlin today and never searches its future."""
    now = datetime(2026, 10, 20, 12, 34, 56, tzinfo=UTC)
    with patch.object(exporter, "fetch_jira_issues", return_value=[]) as fetch:
        assert (
            exporter.load_issues("unused", True, "SDAX", "2025-01-01", None, 100, now=lambda: now)
            == []
        )
    assert fetch.call_args.args[1] == now


def test_export_empty_selection_and_publication_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty saved selection never fetches and failed publication preserves output."""
    import file_io

    source, output = tmp_path / "issues.json", tmp_path / "countries.xlsx"
    source.write_text("[]")
    with patch.object(exporter, "_fetch_asset_with_retry", side_effect=AssertionError("network")):
        assert exporter.main(["--input", str(source), "--output", str(output)]) == 0
    assert pd.read_excel(output).columns.tolist() == ["Jira Issue ID", "Country"]
    before = output.read_bytes()

    def fail(*args: object) -> None:
        raise OSError("SYNTHETIC_PRIVATE_MARKER")

    monkeypatch.setattr(file_io.os, "replace", fail)
    assert exporter.main(["--input", str(source), "--output", str(output)]) == 3
    assert output.read_bytes() == before
    source.write_text('{"errorMessages": ["SYNTHETIC_PRIVATE_MARKER"]}')
    assert exporter.main(["--input", str(source), "--output", str(output)]) == 2


def _asset(object_id: str, country: str | None) -> dict[str, Any]:
    return {
        "id": object_id,
        "attributes": (
            []
            if country is None
            else [
                {
                    "objectTypeAttribute": {"name": "Land"},
                    "objectAttributeValues": [{"displayValue": country}],
                }
            ]
        ),
    }


def test_export_failure_can_recover_and_returns_nonzero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A fallback country retains earlier failure and a later invocation recovers."""
    import json

    tenant = load_settings({}).tenant
    record: dict[str, Any] = {
        "key": "SDAX-1",
        "fields": {
            "customfield_10689": [{"workspaceId": tenant.workspace_id, "objectId": "1"}],
            "customfield_10674": [{"workspaceId": tenant.workspace_id, "objectId": "2"}],
        },
    }
    source, output = tmp_path / "issues.json", tmp_path / "countries.xlsx"
    source.write_text(json.dumps([record]))
    failed = True

    def fetch(oid: str, *args: object, **kwargs: Any) -> dict[str, Any]:
        if failed and oid == "1":
            raise PermissionError("SYNTHETIC_PRIVATE_MARKER")
        return _asset(oid, "DE" if oid == "2" else None)

    monkeypatch.setattr(exporter, "_fetch_asset_with_retry", fetch)
    assert exporter.main(["--input", str(source), "--output", str(output)]) == 1
    assert pd.read_excel(output, sheet_name="Issue Country").to_dict("records") == [
        {"Jira Issue ID": "SDAX-1", "Country": "DE"}
    ]
    assert "SYNTHETIC_PRIVATE_MARKER" not in capsys.readouterr().out
    failed = False
    assert exporter.main(["--input", str(source), "--output", str(output)]) == 0
    assert not exporter.resolve_country_report([record]).diagnostics
    record["fields"]["customfield_10674"] = []
    report = exporter.resolve_country_report([record])
    assert report.countries == {"SDAX-1": None} and not report.diagnostics


@pytest.mark.parametrize("kind", ["modern", "legacy", "foreign", "multi"])
def test_country_adapters_agree_and_keep_workbook_contract(kind: str) -> None:
    """Shared provenance, first-reference selection and escalation agree offline."""
    from datetime import datetime

    import asset_country
    from country_cache import AssetIdentity, CountryCache, CountryEntry
    from data_transformation import load_project_issues

    tenant = load_settings({}).tenant
    refs = [{"workspaceId": tenant.workspace_id, "objectId": "1"}]
    record: dict[str, Any] = {"key": "SDAX-1", "fields": {"customfield_10689": refs}}
    if kind == "modern":
        record.update(assets_cloud_id=tenant.cloud_id, assets_workspace_id=tenant.workspace_id)
    elif kind == "legacy":
        refs[0].pop("workspaceId")
    else:
        refs[0]["workspaceId"] = "foreign"
        if kind == "multi":
            refs.append({"workspaceId": tenant.workspace_id, "objectId": "2"})
            record["fields"]["customfield_10674"] = [refs[-1]]
    cache = CountryCache(
        {
            AssetIdentity(tenant.cloud_id, tenant.workspace_id, oid): CountryEntry(
                AssetIdentity(tenant.cloud_id, tenant.workspace_id, oid),
                "found",
                country,
                datetime.now(UTC),
            )
            for oid, country in (("1", "DE"), ("2", "AT"))
        }
    )
    frame = load_project_issues("SDAX", [record], country_cache=cache)
    displayed = asset_country.attach_country(frame, cache, allow_legacy=True).iloc[0].Land
    with patch.object(
        exporter,
        "_fetch_asset_with_retry",
        side_effect=lambda oid, *a, **k: _asset(oid, "DE" if oid == "1" else "AT"),
    ):
        report = exporter.resolve_country_report([record])
        workbook = exporter.build_country_frame([record])
    assert workbook.columns.tolist() == ["Jira Issue ID", "Country"]
    assert report.countries["SDAX-1"] == (
        None if displayed == asset_country.NOT_RESOLVED else displayed
    )


class CountryExportTests(unittest.TestCase):
    def test_foreign_workspace_does_not_abort_other_tickets_or_use_foreign_id(self):
        issues = [
            {
                "key": "SDEU-1",
                "fields": {"customfield_10673": [{"workspaceId": "sandbox", "objectId": "1"}]},
            },
            {
                "key": "SDAX-2",
                "fields": {
                    "customfield_10673": [{"workspaceId": exporter.WORKSPACE_ID, "objectId": "2"}]
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
            patch.object(exporter, "fetch_asset_object", return_value={"id": "other"}) as fetch,
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
