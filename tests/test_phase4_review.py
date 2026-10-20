"""Regression cases for the independent phase-four preservation findings."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pandas as pd
import pytest
from test_link_enrichment import outward
from test_sync_service import raw

import asset_country
import country_cache
import desk_sync
import jira_loader
from country_cache import AssetIdentity, CountryCache, CountryEntry
from data_loading import cache_revision
from data_transformation import load_project_issues
from file_io import AtomicWriteError
from issue_links import IssueLink, LinkGraph
from settings import load_settings


@pytest.mark.parametrize("operation", ["merge", "migrate"])
def test_country_alias_keeps_one_canonical_artifact(
    tmp_path: Path, operation: str
) -> None:
    """An alias shares canonical publication, backups and subsequent writers."""
    canonical = tmp_path / "countries.json"
    alias = tmp_path / "alias.json"
    tenant = load_settings({}).tenant
    identity = AssetIdentity(tenant.cloud_id, tenant.workspace_id, "41")
    original = b'{"41":"DE"}'
    canonical.write_bytes(original)
    alias.symlink_to(canonical)
    if operation == "merge":
        entry = CountryEntry(identity, "found", "FR", datetime(2026, 10, 2, tzinfo=UTC))
        country_cache.merge_country_entries(alias, [entry])
        assert country_cache.load_country_cache(canonical).entries[identity] == entry
        other = CountryEntry(
            AssetIdentity(tenant.cloud_id, tenant.workspace_id, "42"),
            "found",
            "AT",
            datetime(2026, 10, 3, tzinfo=UTC),
        )
        country_cache.merge_country_entries(canonical, [other])
        assert len(country_cache.load_country_cache(alias).entries) == 2
    else:
        revision = cache_revision(alias)
        assert revision is not None
        assert (
            country_cache.main(
                [
                    "migrate",
                    "--path",
                    str(alias),
                    "--apply",
                    "--expected-revision",
                    ":".join(map(str, revision)),
                ]
            )
            == 0
        )
        assert json.loads(canonical.read_text())["version"] == 2
    assert alias.is_symlink()
    backups = list(tmp_path.glob("countries.json.bak-country-*"))
    assert len(backups) == 1 and backups[0].read_bytes() == original
    assert not list(tmp_path.glob("alias.json.bak-*"))


def test_country_completion_timestamp_precedes_ordered_consumption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fast old response blocked behind another lookup cannot outrank a newer writer."""
    config = load_settings({})
    identity = AssetIdentity(config.tenant.cloud_id, config.tenant.workspace_id, "2")
    frame = pd.DataFrame(
        [{"key": "SDAX-1", "ansprechpartner": "ID_1", "filiale": "ID_2"}]
    )
    frame.attrs["evex_jira_tenant"] = config.tenant.as_dict()
    path = tmp_path / "countries.json"
    first = datetime(2026, 10, 1, tzinfo=UTC)
    second = datetime(2026, 10, 2, tzinfo=UTC)
    third = datetime(2026, 10, 3, tzinfo=UTC)
    fast_done, release_slow = threading.Event(), threading.Event()
    requested = threading.Event()
    prepared: list[CountryCache] = []
    errors: list[BaseException] = []

    class ObservedPool(ThreadPoolExecutor):
        """Expose actual future completion, after the worker has returned."""

        def submit(self, fn: Any, /, *args: Any, **kwargs: Any) -> Any:
            """Observe the fast identity without changing ordered map behavior."""
            future = super().submit(fn, *args, **kwargs)
            if args[0].object_id == "2":
                future.add_done_callback(lambda _: fast_done.set())
            return future

    monkeypatch.setattr(asset_country, "ThreadPoolExecutor", ObservedPool)

    def clock() -> datetime:
        if threading.current_thread().name == "country-review-main":
            if not requested.is_set():
                requested.set()
                return first
            return third
        return first if not release_slow.is_set() else third

    def resolve(oid: str) -> str:
        if oid == "1":
            assert release_slow.wait(5)
            return "Other"
        return "Old"

    def refresh() -> None:
        try:
            _, result, _ = asset_country.refresh_countries(
                frame,
                cache=CountryCache(),
                resolver=resolve,
                workers=2,
                now=clock,
                settings=config,
            )
            prepared.append(result)
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=refresh, name="country-review-main")
    thread.start()
    try:
        assert fast_done.wait(5)
        # The old response has completed while the ordered first result is blocked.
        country_cache.merge_country_entries(
            path, [CountryEntry(identity, "found", "New", second)]
        )
    finally:
        release_slow.set()
        thread.join(5)
    assert not thread.is_alive() and not errors
    assert prepared[0].entries[identity].fetched_at == first
    merged = country_cache.merge_country_entries(
        path, list(prepared[0].entries.values())
    )
    assert merged.entries[identity].country == "New"


@pytest.mark.parametrize("changed_direct", [False, True])
def test_mixed_link_outcomes_keep_a_coherent_previous_snapshot(
    storage: Path,
    changed_direct: bool,
) -> None:
    """Partial success cannot mix old flags with new text or borrow changed provenance."""
    record = raw()
    record["fields"]["issuelinks"] = [outward("EXAX-1"), outward("EXAX-2")]
    old = load_project_issues(
        "SDAX",
        [record],
        link_graph=LinkGraph(
            {
                "EXAX-1": (IssueLink("Relates", "outward", "OTHER-1"),),
                "EXAX-2": (IssueLink("Cloners", "outward", "AX-9"),),
            },
            (),
        ),
    )
    if changed_direct:
        record["fields"]["issuelinks"][1] = outward("EXAX-3")
    partial = load_project_issues(
        "SDAX",
        [record],
        link_graph=LinkGraph(
            {
                "EXAX-1": (IssueLink("Relates", "outward", "OTHER-3"),),
            },
            (),
        ),
    )
    row = desk_sync._keep_known_links(old, partial).iloc[0]
    assert not bool(row.link_enrichment_complete)
    if changed_direct:
        assert row.clones_of_clones == "OTHER-3" and pd.isna(row.has_axt_clone_clone)
    else:
        assert row.clones_of_clones == "OTHER-1,AX-9"
        assert row.clone_types_of_clones == "Relates,Cloners" and bool(
            row.has_axt_clone_clone
        )


def test_optional_raw_publication_failure_preserves_previous_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lower-level raw adapter also uses the neutral atomic publisher."""
    target = tmp_path / "raw.json"
    previous = b'["previous diagnostic snapshot"]'
    target.write_bytes(previous)
    config = load_settings(
        {
            "JIRA_URL": "https://jira.example",
            "JIRA_USERNAME": "test",
            "JIRA_PASSWORD": "synthetic",
        }
    )
    client = Mock()
    client.myself.return_value = {"timeZone": "UTC"}
    client.enhanced_search_issues.return_value = {"issues": []}
    monkeypatch.setattr(jira_loader, "JIRA", Mock(return_value=client))

    def interrupted(*args: Any, **kwargs: Any) -> Any:
        yield "["
        raise OSError("synthetic interrupted serializer")

    monkeypatch.setattr(json.JSONEncoder, "iterencode", interrupted)
    with pytest.raises((OSError, AtomicWriteError)):
        jira_loader.fetch_jira_issues(project="SDAX", save_path=target, settings=config)
    assert target.read_bytes() == previous
    assert not list(tmp_path.glob(".evex-atomic-*.tmp"))
