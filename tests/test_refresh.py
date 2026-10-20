import json
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock

import pandas as pd
import pytest
from test_sync_service import END, START, raw, request

import desk_sync
import source_sync
import sync_checkpoints


def test_sidebar_raw_snapshot_combines_successful_desks_and_accepts_zero_results(
    storage, monkeypatch
):
    monkeypatch.setattr(
        sync_checkpoints,
        "fetch_jira_issues",
        lambda *a, project, **k: [] if project == "SDAX" else [raw(project)],
    )
    (storage.parent / "jira_issues.json").write_text('["previous"]')
    report = desk_sync.run_sync(request(publish_raw=True, repair_sources=True))
    assert report.complete
    assert report.counts == {"SDIPR": 1, "SDAX": 0, "SDEU": 1}
    snapshot = json.loads((storage.parent / "jira_issues.json").read_text())
    assert [i["key"] for i in snapshot] == ["SDIPR-1", "SDEU-1"]
    assert set(pd.read_pickle(storage).key) == {"SDIPR-1", "SDEU-1"}


def test_only_explicit_source_policy_repairs_missing_origins(storage, monkeypatch):
    fresh = raw()
    fresh["fields"]["customfield_10675"] = {"value": ""}
    monkeypatch.setattr(sync_checkpoints, "fetch_jira_issues", lambda *a, **k: [fresh])
    queried = []

    def fields(keys, wanted, **kwargs):
        queried.extend(keys)
        assert wanted == ["customfield_10675"]
        return {key: {"customfield_10675": {"value": "Telefon"}} for key in keys}

    monkeypatch.setattr(source_sync, "fetch_issue_fields", fields)
    assert desk_sync.run_sync(request(projects=["SDAX"])).complete
    assert pd.read_pickle(storage).loc[0, "source"] == "" and not queried
    report = desk_sync.run_sync(request(projects=["SDAX"], repair_sources=True))
    assert report.complete and report.source_updates == 1
    assert pd.read_pickle(storage).loc[0, "source"] == "Telefon"
    assert queried == ["SDAX-1"]


def test_merge_and_resume_keep_newer_raw_snapshot(
    storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Partial resume and ordinary refresh use identical UTC recency rules."""
    old, new = raw(), raw()
    old["fields"]["updated"] = "2026-09-01T12:00:00+02:00"
    new["fields"]["updated"] = "2026-09-01T11:00:00Z"
    old["fields"]["summary"] = "older"
    new["fields"]["summary"] = "newer"
    store = sync_checkpoints.CheckpointStore(storage.parent / "resume")
    store.begin_partial("SDAX", START, END)([new, old])
    loaded, _, _ = store.load_partial("SDAX", START)
    assert loaded == [new]
    store.fetch_jira_issues = Mock(return_value=[old])
    assert store.fetch_project("SDAX", START, END + timedelta(days=1), True) == [new]
    monkeypatch.setattr(
        sync_checkpoints, "fetch_jira_issues", lambda *a, **k: [old, new]
    )
    assert desk_sync.run_sync(request(projects=["SDAX"])).complete
    assert pd.read_pickle(storage).loc[0, "summary"] == "newer"
