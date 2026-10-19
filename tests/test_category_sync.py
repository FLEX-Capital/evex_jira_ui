from unittest.mock import Mock, patch

import pandas as pd
import pytest
import requests

import jira_loader as loader


def frame():
    return pd.DataFrame(
        [
            {
                "key": "SDAX-1",
                "main_category_id": "41",
                "sub_category_id": "42",
                "Hauptkategorie": "Unbekannt",
                "Unterkategorie": "Unknown",
                "updated": "2026-09-01T10:00:00Z",
                "source": "Portal",
            },
            {
                "key": "SDAX-2",
                "main_category_id": "41",
                "sub_category_id": "",
                "Hauptkategorie": None,
                "Unterkategorie": "Unbekannt",
                "updated": "2026-09-01T10:00:00Z",
                "source": "Telefon",
            },
        ]
    )


def references():
    return {
        key: {
            "updated": "2026-09-01T11:00:00Z",
            "customfield_10680": [
                {"workspaceId": loader.WORKSPACE_ID, "objectId": "41"}
            ],
            "customfield_10679": (
                [{"workspaceId": loader.WORKSPACE_ID, "objectId": "42"}]
                if key == "SDAX-1"
                else []
            ),
        }
        for key in ("SDAX-1", "SDAX-2")
    }


def test_unknown_categories_are_repaired_once_per_identity_and_unassigned_is_distinct():
    import category_sync as categories

    fetch = Mock(
        side_effect=lambda c, w, oid: {
            "id": oid,
            "label": "Example main" if oid == "41" else "Example sub",
        }
    )
    resolver = categories.CategoryResolver(fetch=fetch)
    original = frame()
    patches, report = categories.prepare_category_repairs(
        original, references=references(), resolver=resolver
    )
    result, skipped = categories.apply_category_repairs(original, patches)
    assert result.Hauptkategorie.tolist() == ["Example main", "Example main"]
    assert result.Unterkategorie.tolist() == ["Example sub", "Nicht zugeordnet"]
    assert result.source.tolist() == ["Portal", "Telefon"]
    assert fetch.call_count == 2
    assert report.unresolved_cells == skipped == 0
    assert report.repaired_cells == 3 and report.unassigned_cells == 1


def test_cached_ids_are_verified_and_missing_fields_are_not_assumed_unassigned():
    import category_sync as categories

    refs = references()
    del refs["SDAX-2"]["customfield_10679"]
    read = Mock(return_value=refs)
    patches, report = categories.prepare_category_repairs(
        frame(),
        reference_loader=read,
        resolver=categories.CategoryResolver(
            fetch=lambda c, w, oid: {"id": oid, "label": oid}
        ),
    )
    result, _ = categories.apply_category_repairs(frame(), patches)
    assert set(read.call_args.args[0]) == {"SDAX-1", "SDAX-2"}
    assert result.loc[1, "Unterkategorie"] == "Unbekannt"
    assert report.unresolved_cells == 1


@pytest.mark.parametrize("workspace", ["foreign", None])
def test_cached_bare_ids_cannot_borrow_normal_workspace_labels(workspace):
    import category_sync as categories

    refs = references()
    refs["SDAX-1"]["customfield_10680"][0]["workspaceId"] = workspace
    refs["SDAX-2"]["customfield_10680"][0]["workspaceId"] = workspace
    fetch = Mock(side_effect=lambda c, w, oid: {"id": oid, "label": "Valid"})
    patches, report = categories.prepare_category_repairs(
        frame(), references=refs, resolver=categories.CategoryResolver(fetch=fetch)
    )
    result, _ = categories.apply_category_repairs(frame(), patches)
    assert result.loc[0, "Hauptkategorie"] == "Unbekannt"
    assert pd.isna(result.loc[1, "Hauptkategorie"])
    assert report.unresolved_cells == 2
    assert all(call.args[2] != "41" for call in fetch.call_args_list)


def test_retry_budget_is_shared_across_tickets_and_resets_next_run():
    import category_sync as categories

    fetch = Mock(side_effect=requests.Timeout())
    delays = []
    resolver = categories.CategoryResolver(fetch=fetch, sleep=delays.append)
    for _ in range(5):
        assert (
            resolver.resolve(loader.CLOUD_ID, loader.WORKSPACE_ID, "41").label is None
        )
    assert fetch.call_count == 3
    assert delays == [1, 2]
    fetch.side_effect = None
    fetch.return_value = {"id": "41", "label": "Recovered"}
    assert (
        categories.CategoryResolver(fetch=fetch)
        .resolve(loader.CLOUD_ID, loader.WORKSPACE_ID, "41")
        .label
        == "Recovered"
    )


@pytest.mark.parametrize("status,attempts", [(403, 1), (404, 1), (503, 3), (429, 3)])
def test_http_failures_have_bounded_retries_and_honour_retry_after(status, attempts):
    import category_sync as categories

    response = requests.Response()
    response.status_code = status
    response.headers["Retry-After"] = "7"
    fetch = Mock(side_effect=requests.HTTPError(response=response))
    delays = []
    value = categories.CategoryResolver(fetch=fetch, sleep=delays.append).resolve(
        loader.CLOUD_ID, loader.WORKSPACE_ID, "41"
    )
    assert value.label is None and value.attempts == attempts
    assert fetch.call_count == attempts
    assert delays == [7] * (attempts - 1)


def test_transient_failure_recovers_without_restarting_other_enrichment():
    import category_sync as categories

    fetch = Mock(
        side_effect=[requests.ConnectionError(), {"id": "41", "label": "Recovered"}]
    )
    resolver = categories.CategoryResolver(fetch=fetch, sleep=lambda seconds: None)
    raw = {
        "fields": {
            "customfield_10680": references()["SDAX-1"]["customfield_10680"],
            "customfield_10679": [],
            "customfield_10673": [],
        }
    }
    enriched = loader.enrich_issue_assets(raw, category_resolver=resolver)
    assert enriched["asset_labels"]["41"] == "Recovered"
    assert enriched["asset_errors"] == []
    assert resolver.resolve(loader.CLOUD_ID, loader.WORKSPACE_ID, "41").attempts == 2


@pytest.mark.parametrize("response", [{"id": "other", "label": "Wrong"}, {"id": "41"}])
def test_wrong_identity_or_unlabeled_object_is_not_a_success(response):
    import category_sync as categories

    fetch = Mock(return_value=response)
    value = categories.CategoryResolver(fetch=fetch).resolve(
        loader.CLOUD_ID, loader.WORKSPACE_ID, "41"
    )
    assert value.label is None and value.error
    assert fetch.call_count == 1


def test_repair_patches_keep_concurrent_labels_and_newer_jira_snapshots():
    import category_sync as categories

    original = frame()
    patches, _ = categories.prepare_category_repairs(
        original,
        references=references(),
        resolver=categories.CategoryResolver(
            fetch=lambda c, w, oid: {"id": oid, "label": "Recovered"}
        ),
    )
    current = original.copy()
    current.loc[0, "Hauptkategorie"] = "Concurrent label"
    current.loc[1, "updated"] = "2026-09-02T00:00:00Z"
    result, skipped = categories.apply_category_repairs(current, patches)
    assert result.loc[0, "Hauptkategorie"] == "Concurrent label"
    assert pd.isna(result.loc[1, "Hauptkategorie"])
    assert result.loc[0, "Unterkategorie"] == "Recovered"
    assert skipped == 3


def test_changed_reference_updates_id_and_label_together():
    import category_sync as categories

    refs = references()
    refs["SDAX-1"]["customfield_10680"][0]["objectId"] = "99"
    patches, _ = categories.prepare_category_repairs(
        frame(),
        references=refs,
        resolver=categories.CategoryResolver(
            fetch=lambda c, w, oid: {"id": oid, "label": f"Category {oid}"}
        ),
    )
    result, _ = categories.apply_category_repairs(frame(), patches)
    assert result.loc[0, "main_category_id"] == "99"
    assert result.loc[0, "Hauptkategorie"] == "Category 99"


def test_category_repair_rejects_a_concurrent_unrelated_reference():
    import category_sync as categories

    patches, _ = categories.prepare_category_repairs(
        frame(),
        references=references(),
        resolver=categories.CategoryResolver(
            fetch=lambda c, w, oid: {"id": oid, "label": "Recovered"}
        ),
    )
    current = frame()
    current.loc[0, "main_category_id"] = "concurrent-id"
    result, skipped = categories.apply_category_repairs(current, patches)
    assert result.loc[0, "main_category_id"] == "concurrent-id"
    assert result.loc[0, "Hauptkategorie"] == "Unbekannt"
    assert skipped == 1


def test_verified_empty_raw_field_is_unassigned_in_current_schema():
    from test_transformation import issue

    import category_sync as categories
    from data_transformation import load_project_issues

    raw = issue(main_id=None, sub_id=None)
    raw["fields"]["customfield_10673"] = []
    raw["fields"]["customfield_10674"] = []
    enriched = loader.enrich_issue_assets(
        raw, category_resolver=categories.CategoryResolver(fetch=Mock())
    )
    row = load_project_issues("SDEU", [enriched]).iloc[0]
    assert row.Hauptkategorie == row.Unterkategorie == "Nicht zugeordnet"


def test_category_reference_read_pages_selected_fields_without_inventing_absent_issues():
    import category_sync as categories
    import source_sync

    session = Mock()
    first = Mock()
    first.json.return_value = {
        "issues": [{"key": "SDAX-1", "fields": {"customfield_10680": []}}],
        "nextPageToken": "page2",
    }
    last = Mock()
    last.json.return_value = {
        "issues": [{"key": "SDAX-2", "fields": {"updated": "2026-09-01T00:00:00Z"}}]
    }
    session.post.side_effect = [first, last]
    session.__enter__ = Mock(return_value=session)
    session.__exit__ = Mock(return_value=None)
    with patch.object(source_sync.requests, "Session", return_value=session):
        values = categories.fetch_category_references(["SDAX-1", "SDAX-2", "SDAX-3"])
    assert set(values) == {"SDAX-1", "SDAX-2"}
    assert "customfield_10679" not in values["SDAX-2"]
    fields = session.post.call_args.kwargs["json"]["fields"]
    assert set(fields) == {"customfield_10680", "customfield_10679", "updated"}
