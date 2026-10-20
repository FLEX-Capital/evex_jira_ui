"""Link enrichment is bounded, explicit and isolated from ticket conversion."""

from collections.abc import Sequence
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pandas as pd
import pytest
import requests
from test_sync_service import raw, request
from test_transformation import issue


def outward(key: str, kind: str = "Cloners") -> dict[str, Any]:
    """Build a raw outward Jira relationship."""
    return {"type": {"name": kind}, "outwardIssue": {"key": key}}


def test_shared_targets_are_fetched_once_per_import() -> None:
    """Reuse supplied records, deduplicate shared targets and stop at one hop."""
    from issue_links import fetch_link_graph

    calls: list[list[str]] = []
    supplied = [
        issue(key="SDAX-1", links=[outward("SDAX-2"), outward("EXAX-1")]),
        issue(key="SDAX-2", links=[outward(f"EXAX-{i}") for i in range(1, 204)]),
    ]

    def fetch(keys: Sequence[str]) -> dict[str, dict[str, Any]]:
        calls.append(list(keys))
        return {key: {"issuelinks": [outward("DO-NOT-FETCH-1")]} for key in keys}

    graph = fetch_link_graph(supplied, fetch_many=fetch, operation_id="run-1")
    assert all(len(batch) <= 100 and batch == sorted(batch) for batch in calls)
    flattened = [key for batch in calls for key in batch]
    assert len(flattened) == len(set(flattened)) == 203
    assert "SDAX-2" not in flattened and "DO-NOT-FETCH-1" not in flattened
    assert graph.by_key["SDAX-2"][0].target_key == "EXAX-1"
    assert not graph.diagnostics


def test_import_reuses_target_across_desks(
    storage: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The per-operation target cache spans independently transformed desks."""
    import desk_sync
    import source_sync
    import sync_checkpoints

    records = {project: raw(project) for project in ("SDAX", "SDEU")}
    for value in records.values():
        value["fields"]["issuelinks"] = [outward("EXAX-1")]
    monkeypatch.setattr(
        sync_checkpoints,
        "fetch_jira_issues",
        lambda *a, project, **k: [records[project]],
    )
    calls: list[list[str]] = []

    def fetch(keys: Sequence[str], fields: list[str], **kwargs: Any) -> dict[str, dict]:
        calls.append(list(keys))
        assert fields == ["issuelinks"]
        return {key: {"issuelinks": []} for key in keys}

    monkeypatch.setattr(source_sync, "fetch_issue_fields", fetch)
    report = desk_sync.run_sync(request(projects=["SDAX", "SDEU"]))
    assert report.complete and calls == [["EXAX-1"]]


def test_transformation_never_constructs_a_network_client(storage: Path) -> None:
    """Direct links and explicit second-hop evidence survive offline conversion."""
    from data_transformation import load_project_issues
    from issue_links import IssueLink, LinkGraph

    record = issue(key="SDAX-1", links=[outward("EXAX-1", "Blocks")])
    graph = LinkGraph({"EXAX-1": (IssueLink("Relates", "outward", "AX-9"),)}, ())
    with patch("data_transformation.get_jira", side_effect=AssertionError("network")):
        row = load_project_issues("SDAX", [record], link_graph=graph).iloc[0]
        unknown = load_project_issues("SDAX", [record]).iloc[0]
    assert row.clones == "EXAX-1" and row.clone_types == "Blocks"
    assert row.clones_of_clones == "AX-9" and bool(row.link_enrichment_complete)
    assert not bool(unknown.link_enrichment_complete)
    assert pd.isna(unknown.clones_of_clones) and pd.isna(unknown.has_axt_clone_clone)


def test_legacy_link_text_keeps_repeated_records(storage: Path) -> None:
    """Normalized relationship deduplication does not rewrite legacy text counts."""
    from data_transformation import load_project_issues
    from issue_links import LinkGraph

    record = issue(key="SDAX-1", links=[outward("EXAX-1"), outward("EXAX-1")])
    row = load_project_issues("SDAX", [record], link_graph=LinkGraph({"EXAX-1": ()}, ())).iloc[0]
    assert row.clones == "EXAX-1,EXAX-1" and row.n_clones == 2
    assert row.clone_count == 1


def test_optional_link_failure_keeps_valid_ticket_and_other_desks(
    storage: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An optional timeout keeps tickets, previous evidence and other desks."""
    import data_loading
    import desk_sync
    import source_sync
    import sync_checkpoints
    from data_transformation import load_project_issues
    from issue_links import IssueLink, LinkGraph

    first = raw()
    first["fields"]["issuelinks"] = [outward("EXAX-1")]
    prior = load_project_issues(
        "SDAX",
        [first],
        link_graph=LinkGraph({"EXAX-1": (IssueLink("Cloners", "outward", "AX-9"),)}, ()),
    )
    data_loading.save_data(prior)
    monkeypatch.setattr(
        sync_checkpoints,
        "fetch_jira_issues",
        lambda *a, project, **k: [first if project == "SDAX" else raw(project)],
    )

    def fail(*args: object, **kwargs: object) -> dict:
        raise requests.Timeout("SYNTHETIC_PRIVATE_MARKER")

    monkeypatch.setattr(source_sync, "fetch_issue_fields", fail)
    report = desk_sync.run_sync(request(projects=["SDAX", "SDEU"]))
    result = pd.read_pickle(storage).set_index("key")
    assert set(result.index) == {"SDAX-1", "SDEU-1"}
    assert result.loc["SDAX-1", "clones_of_clones"] == "AX-9"
    assert report.link_failures == {"SDAX": 1, "SDEU": 0}
    assert report.committed and not report.complete
    assert report.diagnostics[0].stage == "links"


def test_unknown_legacy_relationship_identity_does_not_reject_new_ticket(
    storage: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unknown old provenance prevents inheritance without breaking import."""
    import data_loading
    import desk_sync
    import source_sync
    import sync_checkpoints

    data_loading.save_data(pd.DataFrame([{"key": "SDAX-1", "summary": "old"}]))
    record = raw()
    record["fields"]["issuelinks"] = [outward("EXAX-1")]
    monkeypatch.setattr(sync_checkpoints, "fetch_jira_issues", lambda *a, **k: [record])
    monkeypatch.setattr(source_sync, "fetch_issue_fields", lambda *a, **k: {})
    report = desk_sync.run_sync(request(projects=["SDAX"]))
    assert report.committed and not report.save_error
    assert pd.read_pickle(storage).iloc[0].summary == record["fields"]["summary"]
