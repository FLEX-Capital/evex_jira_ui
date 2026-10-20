"""Regressions for the independent configuration and tenant-boundary review."""

import sys
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pandas as pd
import pytest
from test_transformation import issue

import backfill_country
import data_loading
import data_transformation
from settings import TENANT_ATTR, SettingsError, load_settings


def test_ticket_defaults_follow_the_supplied_settings_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rotated environment cannot redirect any part of a captured transaction."""
    first, later = tmp_path / "first", tmp_path / "later"
    monkeypatch.setenv("DATA_DIR", str(first))
    captured = load_settings()
    monkeypatch.setenv("DATA_DIR", str(later))
    frame = pd.DataFrame([{"key": "SDAX-1"}])
    data_loading.save_data(frame, settings=captured)
    assert captured.paths.tickets.exists()
    assert data_loading.load_data(settings=captured).key.tolist() == ["SDAX-1"]
    loaded, revision = data_loading.load_data_snapshot(settings=captured)
    loaded.loc[0, "summary"] = "Updated"
    data_loading.save_data_if_unchanged(loaded, revision, settings=captured)
    assert data_loading.load_data(settings=captured).loc[0, "summary"] == "Updated"
    assert not later.exists()


@pytest.mark.parametrize("provenance", ["none", "workspace_only"])
def test_nondefault_cloud_cannot_inherit_unproven_asset_labels(
    monkeypatch: pytest.MonkeyPatch, provenance: str
) -> None:
    """A matching object and workspace never supplies missing cloud ownership."""
    original = load_settings()
    value = issue(main_id="41", sub_id=None, asset_labels={"41": "Other cloud"})
    if provenance == "workspace_only":
        value["assets_workspace_id"] = original.tenant.workspace_id
    monkeypatch.setenv("JIRA_CLOUD_ID", "11111111-1111-4111-8111-111111111111")
    frame = data_transformation.load_project_issues("SDEU", [value])
    assert frame.loc[0, "Hauptkategorie"] == "Unbekannt"


def test_country_backfill_rejects_foreign_ticket_ownership_before_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An alternate output must not bypass validation of the input's tenant."""
    source, output = tmp_path / "foreign.pkl", tmp_path / "result.pkl"
    frame = pd.DataFrame([{"key": "SDEU-1", "ansprechpartner": "ID_41"}])
    identity = load_settings().tenant.as_dict()
    identity["workspace_id"] = "11111111-1111-4111-8111-111111111111"
    frame.attrs[TENANT_ATTR] = identity
    frame.to_pickle(source)
    before = source.read_bytes()
    cache = tmp_path / "asset_country.json"
    cache.write_text('{"41":"Default tenant country"}')
    refresh = Mock(wraps=backfill_country.refresh_countries)
    monkeypatch.setattr(backfill_country, "refresh_countries", refresh)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "backfill_country.py",
            "--input",
            str(source),
            "--output",
            str(output),
            "--cache",
            str(cache),
        ],
    )
    with pytest.raises(SettingsError, match="tenant"):
        backfill_country.main()
    refresh.assert_not_called()
    assert not output.exists()
    assert source.read_bytes() == before
    assert cache.read_text() == '{"41":"Default tenant country"}'


def test_country_backfill_keeps_one_snapshot_through_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rotation during enrichment cannot change cache tenant or lookup credentials."""
    captured = load_settings()
    data_loading.save_data(pd.DataFrame([{"key": "SDEU-1"}]), settings=captured)
    seen = {}

    def refresh(
        frame: pd.DataFrame, **kwargs: Any
    ) -> tuple[pd.DataFrame, dict[str, str | None], dict[str, int]]:
        seen["resolver"] = kwargs["resolver"]
        monkeypatch.setenv("JIRA_WORKSPACE_ID", "11111111-1111-4111-8111-111111111111")
        frame["Land"] = "Keine Assets verknüpft"
        return frame, {}, {"dropped": 0, "resolved": 0, "failed": 0}

    monkeypatch.setattr(backfill_country, "refresh_countries", refresh)
    monkeypatch.setattr(sys, "argv", ["backfill_country.py"])
    backfill_country.main()
    assert seen["resolver"].keywords["settings"] == captured
    assert (
        data_loading.load_data(settings=captured).attrs[TENANT_ATTR]
        == captured.tenant.as_dict()
    )
