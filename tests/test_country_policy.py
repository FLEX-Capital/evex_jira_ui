"""Country policy agrees across identity parsing, extraction and export adapters."""

import sys
from dataclasses import asdict
from unittest.mock import Mock

import pandas as pd
import pytest
import requests
from test_transformation import issue

import asset_country as country
import data_transformation as transform
import jira_country_export as export
import service_desks as desks
from cache_schema import normalize_frame
from settings import load_settings


def test_first_reference_and_workspace_policy_matches_adapters() -> None:
    """First modern reference wins and neither missing nor foreign ownership leaks."""
    from asset_country import parse_asset_reference

    config = load_settings({})
    tenant = config.tenant
    references = [
        {"workspaceId": tenant.workspace_id, "objectId": "1"},
        {"workspaceId": tenant.workspace_id, "objectId": "2"},
    ]
    parsed = parse_asset_reference(references, tenant=tenant)
    assert parsed is not None and parsed.object_id == "1"
    assert parse_asset_reference([{"objectId": "1"}], tenant=tenant) is None
    assert (
        parse_asset_reference([{"workspaceId": "foreign", "objectId": "1"}], tenant=tenant) is None
    )
    assert parse_asset_reference("ID_1", tenant=tenant) is None
    parsed = parse_asset_reference("ID_1", tenant=tenant, legacy=True)
    assert parsed is not None and parsed.object_id == "1"
    value = issue(assets_cloud_id=tenant.cloud_id, assets_workspace_id=tenant.workspace_id)
    value["fields"][desks.FIELDS.contact] = references
    assert (
        export._object_id_for_field(value["fields"], desks.FIELDS.contact, settings=config) == "1"
    )
    frame = transform.load_project_issues("SDEU", [value], country_cache={}, settings=config)
    assert frame.loc[0, "ansprechpartner"] == "ID_1"
    record = next(
        record
        for record in frame.loc[0, "asset_references"]
        if record["source"] == "ansprechpartner"
    )
    assert record["provenance"] == "modern" and record["cloud_id"] == tenant.cloud_id
    assert record["workspace_id"] == tenant.workspace_id and record["object_id"] == "1"


@pytest.mark.parametrize("workspace", [None, "foreign"])
def test_rejected_modern_reference_never_falls_back_to_legacy(
    workspace: str | None,
) -> None:
    """Rejection survives extraction even if a bare-ID cache has a matching key."""
    tenant = load_settings({}).tenant
    value = issue(assets_cloud_id=tenant.cloud_id, assets_workspace_id=tenant.workspace_id)
    value["fields"][desks.FIELDS.contact] = [{"objectId": "41", "workspaceId": workspace}]
    for field in (desks.FIELDS.branch, desks.FIELDS.central):
        value["fields"][field] = []
    frame = transform.load_project_issues("SDEU", [value], country_cache={"41": "Wrong country"})
    assert frame.loc[0, "ansprechpartner"] == ""
    assert frame.loc[0, "Land"] == country.NOT_RESOLVED
    assert frame.loc[0, "asset_references"][0]["provenance"] == "rejected"


@pytest.mark.parametrize(
    "fault", ["permission", "malformed_identity", "malformed_attributes", "timeout"]
)
def test_country_outcomes_distinguish_empty_failure_and_unresolved(fault: str) -> None:
    """Only a verified empty attribute set is no-country; faults keep safe codes."""
    from asset_country import resolve_country
    from country_cache import AssetIdentity

    tenant = load_settings({}).tenant
    identity = AssetIdentity(tenant.cloud_id, tenant.workspace_id, "41")
    empty = resolve_country(
        identity, fetch=lambda identity: {"id": identity.object_id, "attributes": []}
    )
    assert empty.outcome == "no_country" and empty.country is None
    if fault == "permission":
        fetch = Mock(side_effect=PermissionError("SYNTHETIC_PRIVATE_MARKER"))
    elif fault == "timeout":
        fetch = Mock(side_effect=requests.Timeout("SYNTHETIC_PRIVATE_MARKER"))
    elif fault == "malformed_identity":
        fetch = Mock(return_value={"id": "another", "attributes": []})
    else:
        fetch = Mock(return_value={"id": "41", "attributes": "SYNTHETIC_PRIVATE_MARKER"})
    result = resolve_country(identity, fetch=fetch)
    assert result.outcome == "failed" and result.country is None and result.error_code
    assert "SYNTHETIC_PRIVATE_MARKER" not in str(asdict(result))
    assert fetch.call_count == 1


def test_later_real_country_outranks_unresolved_earlier_reference() -> None:
    """Fallback keeps a valid country while retaining earlier incompleteness."""
    from asset_country import CountryLookup, select_country
    from country_cache import AssetIdentity

    tenant = load_settings({}).tenant
    contact = AssetIdentity(tenant.cloud_id, tenant.workspace_id, "1")
    branch = AssetIdentity(tenant.cloud_id, tenant.workspace_id, "2")
    failed = CountryLookup(contact, "failed", None, "permission")
    result = select_country(
        [("ansprechpartner", contact), ("filiale", branch)],
        {contact: failed, branch: CountryLookup(branch, "found", "Deutschland", None)},
    )
    assert result.country == "Deutschland" and result.source == "filiale"
    assert result.failures == (failed,)
    unknown = select_country([("ansprechpartner", None)], {})
    assert unknown.country == country.NOT_RESOLVED and unknown.failures
    assert unknown.failures[0].identity is None


def test_country_core_does_not_import_export_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The core's real default resolver works with the Excel adapter unavailable."""
    monkeypatch.setitem(sys.modules, "jira_country_export", None)
    monkeypatch.setattr(
        country,
        "fetch_country_asset",
        lambda identity, **kw: {"id": identity.object_id, "attributes": []},
    )
    result, resolved, failed = country.resolve_missing(["ID_41"], {})
    assert result == {"41": None} and resolved == 1 and failed == 0


def test_legacy_schema_never_invents_structured_tenant_provenance() -> None:
    """An additive optional column represents old unknown references honestly."""
    frame = normalize_frame(
        pd.DataFrame([{"key": "SDAX-1", "ansprechpartner": "ID_41", "Land": "Österreich"}])
    )
    assert frame.loc[0, "Land"] == "Österreich"
    assert pd.isna(frame.loc[0, "asset_references"])


def test_country_asset_retries_follow_the_country_budget() -> None:
    """The network boundary retries timeout reads with injected waits only."""
    from asset_country import fetch_country_asset
    from country_cache import AssetIdentity

    tenant = load_settings({}).tenant
    identity = AssetIdentity(tenant.cloud_id, tenant.workspace_id, "41")
    fetch = Mock(side_effect=[requests.Timeout("synthetic"), {"id": "41", "attributes": []}])
    delays: list[float] = []
    assert fetch_country_asset(identity, fetch=fetch, sleep=delays.append)["id"] == "41"
    assert delays == [2] and fetch.call_count == 2
