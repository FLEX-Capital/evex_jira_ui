"""Versioned country state refreshes explicitly and merges by full identity."""

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import pandas as pd
import pytest
import requests
from test_transformation import issue

import asset_country
import jira_loader
import service_desks as desks
from country_cache import AssetIdentity
from data_transformation import load_project_issues
from settings import load_settings

NOW = datetime(2026, 10, 20, 12, tzinfo=UTC)


def _identity() -> AssetIdentity:

    tenant = load_settings({}).tenant
    return AssetIdentity(tenant.cloud_id, tenant.workspace_id, "41")


def _frame() -> pd.DataFrame:
    tenant = load_settings({}).tenant
    value = issue(
        assets_cloud_id=tenant.cloud_id, assets_workspace_id=tenant.workspace_id
    )
    value["fields"][desks.FIELDS.contact] = [
        {"workspaceId": tenant.workspace_id, "objectId": "41"}
    ]
    for field in (desks.FIELDS.branch, desks.FIELDS.central):
        value["fields"][field] = []
    frame = load_project_issues("SDEU", [value], country_cache={})
    frame["Land"] = "Deutschland"
    return frame


def test_country_ttl_boundaries_and_force_refresh(tmp_path: Path) -> None:
    """Exact seven-/one-day boundaries expire, and force bypasses fresh entries."""
    from country_cache import CountryCache, CountryEntry, is_country_fresh

    identity = _identity()
    found = CountryEntry(identity, "found", "Deutschland", NOW)
    empty = CountryEntry(identity, "no_country", None, NOW)
    assert is_country_fresh(found, now=NOW + timedelta(days=7, microseconds=-1))
    assert not is_country_fresh(found, now=NOW + timedelta(days=7))
    assert is_country_fresh(empty, now=NOW + timedelta(days=1, microseconds=-1))
    assert not is_country_fresh(empty, now=NOW + timedelta(days=1))
    cache = CountryCache({identity: found}, {})
    resolver = Mock(return_value="Österreich")
    result, _, stats = asset_country.refresh_countries(
        _frame(), cache=cache, resolver=resolver, now=lambda: NOW
    )
    resolver.assert_not_called()
    assert result.loc[0, "Land"] == "Deutschland" and stats["resolved"] == 0
    result, updated, stats = asset_country.refresh_countries(
        _frame(),
        cache=cache,
        resolver=resolver,
        now=lambda: NOW + timedelta(seconds=1),
        force=True,
    )
    assert result.loc[0, "Land"] == "Österreich" and stats["resolved"] == 1
    assert (
        updated.entries[identity].country == "Österreich" and resolver.call_count == 1
    )


def test_rerender_never_refreshes_or_overwrites_stored_legacy_country(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Legacy metadata and stale values remain visible without invented ownership."""
    from country_cache import load_country_cache

    path = tmp_path / "countries.json"
    path.write_text('{"41":"Unverified cache value"}')
    before = path.read_bytes()
    cache = load_country_cache(path)
    frame = pd.DataFrame(
        [{"key": "SDEU-1", "ansprechpartner": "ID_41", "Land": "Stored country"}]
    )
    fetch = Mock(side_effect=AssertionError("rerender fetched"))
    monkeypatch.setattr(asset_country, "fetch_country_asset", fetch)
    result = asset_country.attach_country(frame, cache)
    counts = asset_country.country_cache_status(frame, cache, now=NOW)
    assert counts["unverified"] == 1 and counts["stale"] == 0
    assert result.loc[0, "Land"] == "Stored country" and not cache.entries
    assert cache.legacy_unverified == {"41": "Unverified cache value"}
    fetch.assert_not_called()
    assert path.read_bytes() == before and list(tmp_path.iterdir()) == [path]
    assert frame.loc[0, "Land"] == "Stored country"


def test_failed_refresh_is_not_cached_as_no_country() -> None:
    """A stale verified value survives failure, and a later invocation can recover."""
    from country_cache import CountryCache, CountryEntry

    identity = _identity()
    old = CountryEntry(identity, "found", "Deutschland", NOW - timedelta(days=8))
    cache = CountryCache({identity: old}, {})
    resolver = Mock(side_effect=[requests.Timeout("synthetic"), "Österreich"])
    result, failed_cache, stats = asset_country.refresh_countries(
        _frame(), cache=cache, resolver=resolver, now=lambda: NOW
    )
    assert (
        failed_cache.entries[identity] == old
        and stats["failed"] == 1
        and stats["stale"] == 1
    )
    assert result.loc[0, "Land"] == "Deutschland"
    result, recovered, stats = asset_country.refresh_countries(
        _frame(), cache=failed_cache, resolver=resolver, now=lambda: NOW
    )
    assert stats["failed"] == 0 and result.loc[0, "Land"] == "Österreich"
    assert (
        recovered.entries[identity].country == "Österreich" and resolver.call_count == 2
    )


def test_two_country_writers_preserve_newer_entries(tmp_path: Path) -> None:
    """Process transactions preserve distinct/full identities and newest values."""
    from country_cache import (
        AssetIdentity,
        CountryEntry,
        load_country_cache,
        merge_country_entries,
    )

    path = tmp_path / "countries.json"
    identity = _identity()
    newer = CountryEntry(identity, "found", "New", NOW)
    merge_country_entries(path, [newer])
    script = (
        "from country_cache import AssetIdentity,CountryEntry,merge_country_entries\n"
        "from datetime import datetime\nfrom pathlib import Path\nimport sys\n"
        "identity=AssetIdentity(sys.argv[2],sys.argv[3],sys.argv[4])\n"
        "entry=CountryEntry(identity,'found',sys.argv[5],datetime.fromisoformat(sys.argv[6]))\n"
        "merge_country_entries(Path(sys.argv[1]),[entry])\n"
    )
    other_workspace = "11111111-1111-4111-8111-111111111111"
    records = [
        (identity.workspace_id, "41", "Old", NOW - timedelta(days=1)),
        (identity.workspace_id, "42", "Second", NOW),
        (other_workspace, "41", "Other workspace", NOW),
    ]
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                script,
                str(path),
                identity.cloud_id,
                workspace,
                object_id,
                value,
                timestamp.isoformat(),
            ]
        )
        for workspace, object_id, value, timestamp in records
    ]
    try:
        for process in processes:
            assert process.wait(30) == 0
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait()
    cache = load_country_cache(path)
    assert cache.entries[identity] == newer and len(cache.entries) == 3
    assert (
        cache.entries[AssetIdentity(identity.cloud_id, other_workspace, "41")].country
        == "Other workspace"
    )


def test_equal_timestamp_merge_is_order_independent(tmp_path: Path) -> None:
    """Canonical-record hashing resolves equal timestamps in either write order."""
    from country_cache import CountryEntry, load_country_cache, merge_country_entries

    values = [CountryEntry(_identity(), "found", value, NOW) for value in ("DE", "AT")]
    first, second = tmp_path / "first.json", tmp_path / "second.json"
    for value in values:
        merge_country_entries(first, [value])
    for value in reversed(values):
        merge_country_entries(second, [value])
    assert load_country_cache(first).entries == load_country_cache(second).entries


def test_country_cache_migration_is_explicit_and_backed_up(tmp_path: Path) -> None:
    """Metadata-only migration preserves exact bytes without claiming tenant entries."""
    from country_cache import load_country_cache, main
    from data_loading import cache_revision

    path = tmp_path / "countries.json"
    path.write_bytes(b'{ "41" : "Deutschland", "42": null }\n')
    before = path.read_bytes()
    assert load_country_cache(path).legacy_unverified == {
        "41": "Deutschland",
        "42": None,
    }
    assert main(["migrate", "--path", str(path)]) == 0
    assert path.read_bytes() == before and list(tmp_path.iterdir()) == [path]
    assert main(["migrate", "--path", str(path), "--apply"]) == 2
    revision = cache_revision(path)
    assert revision is not None
    expected = ":".join(map(str, revision))
    assert (
        main(
            ["migrate", "--path", str(path), "--apply", "--expected-revision", expected]
        )
        == 0
    )
    backups = list(tmp_path.glob("*.bak-country-*"))
    assert len(backups) == 1 and backups[0].read_bytes() == before
    current = load_country_cache(path)
    assert not current.entries and current.legacy_unverified == {
        "41": "Deutschland",
        "42": None,
    }
    assert json.loads(path.read_text())["version"] == 2


@pytest.mark.parametrize("content", ['{"version":99}', '{"version":true}', "not json"])
def test_unsupported_or_corrupt_country_cache_fails_safely(
    tmp_path: Path, content: str
) -> None:
    """Unknown schemas never silently become empty or verified state."""
    from country_cache import CountryCacheError, load_country_cache

    path = tmp_path / "countries.json"
    path.write_text(content)
    with pytest.raises(CountryCacheError):
        load_country_cache(path)
    assert path.read_text() == content


def test_explicit_asset_reads_do_not_reuse_process_global_responses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated explicit reads can observe changed remote country values."""
    fetch = Mock(
        side_effect=[{"id": "41", "country": "DE"}, {"id": "41", "country": "AT"}]
    )
    monkeypatch.setattr(jira_loader, "jira_request", fetch)
    first = jira_loader.fetch_asset_object(object_id="41")
    second = jira_loader.fetch_asset_object(object_id="41")
    assert first != second and fetch.call_count == 2
