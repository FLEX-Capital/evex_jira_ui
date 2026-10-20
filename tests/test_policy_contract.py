"""Desk, field, calendar and tenant identity must have shared authorities."""

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock

import pandas as pd
import pytest
from test_sync_service import raw
from test_transformation import issue

import data_transformation as transform
import jira_loader
import reporting_policy as reporting
import service_desks as desks
from category_sync import CategoryResolver, prepare_category_repairs
from sync_checkpoints import CheckpointStore


def test_each_configured_desk_has_one_transformation_policy(storage: Path) -> None:
    """Registry dispatch and existing adapters agree on every supported desk."""
    expected = {
        "SDIPR": ("IPRO", "EXIPR", None, "\n\n", transform.load_issues),
        "SDAX": (
            "Amparex",
            "EXAX",
            "AX",
            "\n--------------------------------\n",
            transform.load_issues_Amparex,
        ),
        "SDEU": (
            "Euronet",
            None,
            None,
            "\n--------------------------------\n",
            transform.load_issues_Euronet,
        ),
    }
    assert set(desks.BY_KEY) == set(expected)
    for key, (company, clone, second, separator, adapter) in expected.items():
        desk = desks.BY_KEY[key]
        assert (
            desk.company,
            desk.clone_project,
            desk.clone_of_clone_project,
            desk.comment_separator,
        ) == (company, clone, second, separator)
        values = [issue(key=f"{key}-1", main_id=None, sub_id=None)]
        pd.testing.assert_frame_equal(
            transform.load_project_issues(key, values), adapter(values)
        )
    assert desks.BY_KEY["SDEU"].project_id == "12521"
    assert desks.BY_KEY["SDEU"].service_desk_id == "219"


def test_field_mapping_drives_fetch_trim_transform_and_repair(
    storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A replaced field map drives all paths without borrowing old field values."""
    import source_sync

    fields = replace(
        desks.FIELDS, main_category="customfield_99990", source="customfield_99991"
    )
    monkeypatch.setattr(desks, "FIELDS", fields)
    value = issue(main_id="41", sub_id=None)
    value["fields"][fields.main_category] = value["fields"].pop("customfield_10680")
    value["fields"][fields.source] = {"value": "Mapped source"}
    value["fields"]["customfield_10675"] = {"value": "Wrong old source"}
    trimmed = jira_loader.trim_issue(value)
    assert fields.main_category in trimmed["fields"]
    assert "customfield_10680" not in fields.retained()
    assert "status" in fields.retained() and "updated" in fields.retained()
    resolver = CategoryResolver(
        fetch=lambda *a: {"id": "41", "label": "Mapped category"}
    )
    enriched = jira_loader.enrich_issue_assets(trimmed, category_resolver=resolver)
    frame = transform.load_project_issues("SDEU", [enriched])
    assert frame.loc[0, "Hauptkategorie"] == "Mapped category"
    assert frame.loc[0, "source"] == "Mapped source"
    frame.loc[0, "Hauptkategorie"] = "Unbekannt"
    patches, report = prepare_category_repairs(
        frame, references={value["key"]: trimmed["fields"]}, resolver=resolver
    )
    assert report.repaired_cells == 1
    assert any(p.label == "Mapped category" and p.reference_id == "41" for p in patches)
    fetch = Mock(
        return_value={value["key"]: {fields.source: {"value": "Mapped source"}}}
    )
    monkeypatch.setattr(source_sync, "fetch_issue_fields", fetch)
    assert source_sync.fetch_issue_sources([value["key"]]) == {
        value["key"]: "Mapped source"
    }
    assert fetch.call_args.args[1] == [fields.source]


def test_reporting_window_has_one_authority() -> None:
    """Compatibility aliases and default band duration derive from one window."""
    from resolution_bands import HOURS_PER_WORKING_DAY, classify_band

    assert transform.BUS_START is reporting.BUSINESS_START
    assert transform.BUS_END is reporting.BUSINESS_END
    assert HOURS_PER_WORKING_DAY == reporting.WORKING_DAY_HOURS == 10.0
    assert classify_band(10.0, True) == "≤ 1 Arbeitstag"
    assert desks.BERLIN is reporting.BERLIN


@pytest.mark.parametrize("variable", ["JIRA_CLOUD_ID", "JIRA_WORKSPACE_ID"])
def test_checkpoint_tenant_mismatch_cannot_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, variable: str
) -> None:
    """Identical keys/window in another tenant cannot reuse a saved checkpoint."""
    start, end = datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 9, 10, tzinfo=UTC)
    store = CheckpointStore(tmp_path)
    store.fetch_jira_issues = Mock(return_value=[raw()])
    store.fetch_project("SDAX", start, end, False)
    monkeypatch.setenv(variable, "11111111-1111-4111-8111-111111111111")
    other = CheckpointStore(tmp_path)
    fetch = Mock(return_value=[])
    other.fetch_jira_issues = fetch
    with pytest.raises(ValueError, match="tenant|workspace"):
        other.fetch_project("SDAX", start, end, True)
    fetch.assert_not_called()


def test_asset_cache_default_call_is_scoped_to_the_full_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default argument resolution must happen before tenant-aware cache lookup."""
    getattr(jira_loader.fetch_asset_object, "cache_clear")()
    fetch = Mock(side_effect=lambda url, **kw: {"id": "41", "source": url})
    monkeypatch.setattr(jira_loader, "jira_request", fetch)
    first = jira_loader.fetch_asset_object(object_id="41")
    monkeypatch.setenv("JIRA_WORKSPACE_ID", "11111111-1111-4111-8111-111111111111")
    second = jira_loader.fetch_asset_object(object_id="41")
    assert first != second and fetch.call_count == 2
    assert "11111111-1111-4111-8111-111111111111" in second["source"]


def test_legacy_caches_cannot_be_relabelled_as_another_tenant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tenantless legacy artifacts are valid only for their historical tenant."""
    import asset_country
    import data_loading
    from settings import SettingsError

    data_loading.save_data(pd.DataFrame([{"key": "SDAX-1", "summary": "old tenant"}]))
    country = tmp_path / "asset_country.json"
    country.write_text('{"41":"Deutschland"}')
    original = country.read_bytes()
    monkeypatch.setenv("JIRA_WORKSPACE_ID", "11111111-1111-4111-8111-111111111111")
    with pytest.raises(SettingsError, match="tenant"):
        data_loading.load_data()
    assert asset_country.load_cache() == {}
    with pytest.raises(SettingsError, match="tenant"):
        asset_country.save_cache({"41": "Österreich"})
    assert country.read_bytes() == original
    separate = tmp_path / "new-tenant.pkl"
    data_loading.save_data(pd.DataFrame([{"key": "SDAX-2"}]), path=separate)
    assert data_loading.load_data(path=separate).key.tolist() == ["SDAX-2"]


def test_explicit_snapshot_survives_environment_rotation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Operation inputs and artifacts stay bound to their captured tenant."""
    import data_loading
    from settings import SettingsError, load_settings

    config = load_settings()
    monkeypatch.setenv("JIRA_WORKSPACE_ID", "11111111-1111-4111-8111-111111111111")
    frame = transform.load_project_issues("SDAX", [raw()], settings=config)
    assert frame.loc[0, "Hauptkategorie"] == "Main"
    path = tmp_path / "captured.pkl"
    data_loading.save_data(frame, path=path, settings=config)
    assert data_loading.load_data(path=path, settings=config).key.tolist() == ["SDAX-1"]
    with pytest.raises(SettingsError, match="tenant"):
        data_loading.load_data(path=path)
    store = CheckpointStore(tmp_path, settings=config)
    fetch = Mock(return_value=[])
    store.fetch_jira_issues = fetch
    store.fetch_project(
        "SDAX",
        datetime(2026, 9, 1, tzinfo=UTC),
        datetime(2026, 9, 10, tzinfo=UTC),
        False,
    )
    assert fetch.call_args.kwargs["settings"] == config
