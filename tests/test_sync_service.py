import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest
from test_transformation import issue

import data_loading
import desk_sync
import jira_loader
import sync_checkpoints

START = datetime(2026, 9, 1, tzinfo=UTC)
END = datetime(2026, 9, 10, tzinfo=UTC)


def raw(project="SDAX", number=1):
    result = issue(
        key=f"{project}-{number}",
        asset_labels={"main-1": "Main", "sub-1": "Sub"},
        assets_workspace_id=jira_loader.WORKSPACE_ID,
        assets_cloud_id=jira_loader.CLOUD_ID,
    )
    result["fields"]["customfield_10673"] = []
    result["fields"]["customfield_10674"] = []
    return result


def request(**kwargs):
    return desk_sync.make_sync_request(START, END, **kwargs)


def test_all_projects_use_current_transformation_and_commit_with_one_backup(
    storage, monkeypatch
):
    data_loading.save_data(pd.DataFrame([{"key": "OLD-1", "summary": "Keep"}]))
    original = storage.read_bytes()
    monkeypatch.setattr(
        sync_checkpoints, "fetch_jira_issues", lambda *a, project, **k: [raw(project)]
    )
    report = desk_sync.run_sync(request())
    df = pd.read_pickle(storage)
    assert set(df.key) == {"OLD-1", "SDIPR-1", "SDAX-1", "SDEU-1"}
    assert df.loc[df.key == "SDAX-1", "Hauptkategorie"].iloc[0] == "Main"
    assert "resolution_band" in df
    assert report.complete and report.rows == 4
    backups = list(storage.parent.glob(f"{storage.name}.bak-*"))
    assert len(backups) == 1 and backups[0].read_bytes() == original
    assert (
        json.loads((storage.parent / "import_status.json").read_text())["state"]
        == "completed"
    )


def test_zero_result_import_repairs_older_unknowns_outside_selected_window(
    storage, monkeypatch
):
    from data_transformation import load_project_issues

    old = load_project_issues("SDAX", [raw()])
    old.loc[0, "Hauptkategorie"] = "Unbekannt"
    old.loc[0, "created"] = pd.Timestamp("2020-01-01", tz="Europe/Berlin")
    data_loading.save_data(old)
    monkeypatch.setattr(sync_checkpoints, "fetch_jira_issues", lambda *a, **k: [])
    report = desk_sync.run_sync(request(projects=["SDAX"]))
    assert pd.read_pickle(storage).loc[0, "Hauptkategorie"] == "Resolved"
    assert report.categories.repaired_cells == 1 and report.complete


def test_all_failed_fetches_do_not_touch_ticket_data(storage, monkeypatch):
    data_loading.save_data(
        pd.DataFrame([{"key": "SDAX-1", "Hauptkategorie": "Unbekannt"}])
    )
    original = storage.read_bytes()

    def fail(*a, **k):
        raise RuntimeError("unavailable")

    monkeypatch.setattr(sync_checkpoints, "fetch_jira_issues", fail)
    report = desk_sync.run_sync(request())
    assert storage.read_bytes() == original
    assert not report.complete and len(report.errors) == 3
    assert not list(storage.parent.glob("*.bak-*"))


def test_partial_desk_failure_preserves_cached_rows_and_previous_raw(
    storage, monkeypatch
):
    data_loading.save_data(pd.DataFrame([{"key": "SDAX-1", "summary": "Retain"}]))
    raw_path = storage.parent / "jira_issues.json"
    raw_path.write_text('["previous"]')

    def fetch(*a, project, **k):
        if project == "SDAX":
            raise RuntimeError("denied")
        return [raw(project)]

    monkeypatch.setattr(sync_checkpoints, "fetch_jira_issues", fetch)
    report = desk_sync.run_sync(request(publish_raw=True))
    assert set(pd.read_pickle(storage).key) == {"SDAX-1", "SDIPR-1", "SDEU-1"}
    assert raw_path.read_text() == '["previous"]'
    assert not report.complete and set(report.errors) == {"SDAX"}


def test_concurrent_maintenance_survives_and_eligible_category_patch_still_applies(
    storage, monkeypatch
):
    from data_transformation import load_project_issues

    baseline = load_project_issues("SDAX", [raw()])
    baseline.loc[0, ["Hauptkategorie", "Land", "source"]] = ["Unbekannt", "Germany", ""]
    data_loading.save_data(baseline)

    def fetch(*a, **k):
        probe = """import fcntl,sys
with open(sys.argv[1]+'.lock', 'a+b') as f:
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        print('free')
    except BlockingIOError:
        print('blocked')
"""
        result = subprocess.run(
            [sys.executable, "-c", probe, str(storage)],
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert result.stdout.strip() == "free"
        newer = baseline.copy()
        newer.loc[0, ["Land", "source"]] = ["Austria", "Telefon"]
        data_loading.save_data(newer)
        return [raw()]

    monkeypatch.setattr(sync_checkpoints, "fetch_jira_issues", fetch)
    report = desk_sync.run_sync(request(projects=["SDAX"]))
    current = pd.read_pickle(storage).iloc[0]
    assert current.Land == "Austria" and current.source == "Telefon"
    # The fresh download already resolved this identity; reuse that label.
    assert current.Hauptkategorie == "Main"
    assert report.protected_rows == 1


def test_same_identity_keeps_valid_label_on_failure_but_changed_id_cannot_inherit_it(
    storage, monkeypatch
):
    from data_transformation import load_project_issues

    old = load_project_issues("SDAX", [raw()])
    data_loading.save_data(old)

    def denied(*a):
        raise PermissionError("denied")

    monkeypatch.setattr(jira_loader, "fetch_asset_object_uncached", denied)
    fresh = raw()
    fresh["asset_labels"] = {"sub-1": "Sub"}
    fresh["asset_errors"] = ["customfield_10680/main-1: PermissionError"]
    monkeypatch.setattr(sync_checkpoints, "fetch_jira_issues", lambda *a, **k: [fresh])
    assert not desk_sync.run_sync(request(projects=["SDAX"])).complete
    assert pd.read_pickle(storage).loc[0, "Hauptkategorie"] == "Main"
    fresh["fields"]["customfield_10680"][0]["objectId"] = "changed"
    assert not desk_sync.run_sync(request(projects=["SDAX"])).complete
    assert pd.read_pickle(storage).loc[0, "Hauptkategorie"] == "Unbekannt"


def test_successful_category_repair_clears_its_warning_but_keeps_other_assets_failure(
    storage, monkeypatch
):
    fresh = raw()
    fresh["asset_labels"] = {}
    fresh["asset_errors"] = [
        "customfield_10680/main-1: Timeout",
        "customfield_10673/customer: HTTP 403",
    ]
    monkeypatch.setattr(sync_checkpoints, "fetch_jira_issues", lambda *a, **k: [fresh])
    report = desk_sync.run_sync(request(projects=["SDAX"]))
    assert pd.read_pickle(storage).loc[0, "Hauptkategorie"] == "Resolved"
    assert report.categories.unresolved_cells == 0
    assert report.asset_failures == {"SDAX": 1} and not report.complete


def test_raw_publication_failure_is_distinct_from_failed_cache_commit(
    storage, monkeypatch
):
    monkeypatch.setattr(sync_checkpoints, "fetch_jira_issues", lambda *a, **k: [raw()])

    def fail(*a, **k):
        raise OSError("read only diagnostic path")

    monkeypatch.setattr(desk_sync, "publish_raw_snapshot", fail, raising=False)
    report = desk_sync.run_sync(request(projects=["SDAX"], publish_raw=True))
    assert storage.exists() and len(pd.read_pickle(storage)) == 1
    assert report.raw_error and not report.save_error and not report.complete


def test_save_failure_keeps_checkpoints_and_releases_import_lock(storage, monkeypatch):
    monkeypatch.setattr(sync_checkpoints, "fetch_jira_issues", lambda *a, **k: [raw()])

    def fail(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(data_loading, "save_data_if_unchanged", fail)
    report = desk_sync.run_sync(request(projects=["SDAX"]))
    assert report.save_error and not report.complete
    assert (storage.parent / "backfill_SDAX.json").exists()
    with data_loading.import_operation_lock(storage):
        pass


def test_import_lock_rejects_another_process_and_does_not_hold_cache_lock(storage):
    code = """import sys
from data_loading import import_operation_lock, ImportBusyError
try:
    with import_operation_lock(sys.argv[1]): print('admitted')
except ImportBusyError:
    print('busy')
"""
    with data_loading.import_operation_lock(storage):
        result = subprocess.run(
            [sys.executable, "-c", code, str(storage)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0 and result.stdout.strip() == "busy"
        data_loading.save_data(pd.DataFrame([{"key": "SDAX-1"}]))
    result = subprocess.run(
        [sys.executable, "-c", code, str(storage)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0 and result.stdout.strip() == "admitted"


def test_source_repair_cannot_replace_a_concurrent_source_update(storage, monkeypatch):
    import source_sync
    from data_transformation import load_project_issues

    fresh = raw()
    fresh["fields"]["customfield_10675"] = {"value": ""}
    old = load_project_issues("SDAX", [fresh])
    data_loading.save_data(old)
    monkeypatch.setattr(sync_checkpoints, "fetch_jira_issues", lambda *a, **k: [fresh])
    original = source_sync.refresh_missing_sources

    def read_sources(keys):
        current = old.copy()
        current.loc[0, "source"] = "Telefon"
        data_loading.save_data(current)
        return {"SDAX-1": "Portal"}

    monkeypatch.setattr(
        source_sync,
        "refresh_missing_sources",
        lambda df: original(df, fetch_sources=read_sources),
    )
    report = desk_sync.run_sync(request(projects=["SDAX"], repair_sources=True))
    assert pd.read_pickle(storage).loc[0, "source"] == "Telefon"
    assert report.source_updates == 0


@pytest.mark.parametrize(
    "old_id,old_label", [("main-1", None), ("old-id", "Unbekannt")]
)
def test_resume_applies_recovered_category_to_accepted_checkpoint_row(
    storage, monkeypatch, old_id, old_label
):
    from data_transformation import load_project_issues

    baseline = load_project_issues("SDAX", [raw()])
    baseline.loc[0, ["main_category_id", "Hauptkategorie"]] = [old_id, old_label]
    data_loading.save_data(baseline)
    resumed = raw()
    resumed["asset_labels"].pop("main-1")
    resumed["asset_errors"] = ["customfield_10680/main-1: Timeout"]
    store = sync_checkpoints.CheckpointStore(storage.parent)
    store.begin_partial("SDAX", START, END)([resumed])
    monkeypatch.setattr(sync_checkpoints, "fetch_jira_issues", lambda *a, **k: [])

    report = desk_sync.run_sync(request(projects=["SDAX"], resume=True))

    current = pd.read_pickle(storage).iloc[0]
    assert current.main_category_id == "main-1"
    assert current.Hauptkategorie == "Resolved"
    assert report.complete and report.categories.repaired_cells == 1
    assert report.categories.unresolved_cells == report.categories.skipped_cells == 0
    assert report.asset_failures == {"SDAX": 0}


def test_raw_snapshot_retains_its_own_request_metadata_after_later_cli_run(
    storage, monkeypatch
):
    import hashlib

    monkeypatch.setattr(sync_checkpoints, "fetch_jira_issues", lambda *a, **k: [])
    first = request(projects=["SDAX"], publish_raw=True)
    assert desk_sync.run_sync(first).complete
    raw_path = storage.parent / "jira_issues.json"
    metadata_path = storage.parent / "jira_issues.meta.json"
    metadata = json.loads(metadata_path.read_text())
    assert json.loads(raw_path.read_text()) == []
    assert metadata["request"] == {**first.to_dict(), "projects": ["SDAX"]}
    assert (
        metadata["run_id"]
        == json.loads(desk_sync.status_path(storage).read_text())["run_id"]
    )
    assert metadata["sha256"] == hashlib.sha256(raw_path.read_bytes()).hexdigest()
    assert datetime.fromisoformat(metadata["published_at"]).tzinfo is not None
    original_metadata = metadata_path.read_bytes()
    assert desk_sync.run_sync(request(projects=["SDEU"])).complete
    assert metadata_path.read_bytes() == original_metadata
    assert json.loads(raw_path.read_text()) == []


def test_raw_metadata_failure_is_reported_after_cache_commit(storage, monkeypatch):
    import hashlib

    monkeypatch.setattr(sync_checkpoints, "fetch_jira_issues", lambda *a, **k: [raw()])
    assert desk_sync.run_sync(request(projects=["SDAX"], publish_raw=True)).complete
    original = json.loads((storage.parent / "jira_issues.meta.json").read_text())
    monkeypatch.setattr(
        sync_checkpoints, "fetch_jira_issues", lambda *a, **k: [raw(number=2)]
    )
    write = desk_sync.write_json_atomic

    def fail_metadata(path, value):
        if str(path).endswith("jira_issues.meta.json"):
            raise OSError("metadata unavailable")
        return write(path, value)

    monkeypatch.setattr(desk_sync, "write_json_atomic", fail_metadata)
    report = desk_sync.run_sync(request(projects=["SDAX"], publish_raw=True))
    assert report.committed and report.raw_error == "OSError"
    assert not report.save_error and not report.complete
    assert (
        original["sha256"]
        != hashlib.sha256(
            (storage.parent / "jira_issues.json").read_bytes()
        ).hexdigest()
    )


def test_invalid_rows_preserve_cached_counterpart_and_valid_desk_rows(
    storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rejecting one raw row preserves its cache while other rows can commit."""
    from data_transformation import load_project_issues

    previous = load_project_issues("SDAX", [raw()])
    previous.loc[0, "summary"] = "retained cached value"
    data_loading.save_data(previous)
    bad = raw()
    bad["fields"]["created"] = "SYNTHETIC_PRIVATE_MARKER"
    monkeypatch.setattr(
        sync_checkpoints,
        "fetch_jira_issues",
        lambda *a, project, **k: (
            [bad, raw("SDAX", 2)] if project == "SDAX" else [raw(project)]
        ),
    )
    report = desk_sync.run_sync(request(projects=["SDAX", "SDEU"]))
    result = pd.read_pickle(storage).set_index("key")
    assert result.loc["SDAX-1", "summary"] == "retained cached value"
    assert set(result.index) == {"SDAX-1", "SDAX-2", "SDEU-1"}
    assert report.invalid_rows == {"SDAX": 1}
    assert report.counts == {"SDAX": 1, "SDEU": 1}
    assert report.committed and not report.complete
    status = json.loads((storage.parent / "import_status.json").read_text())
    assert status["state"] == "partial"
    assert status["report"]["invalid_rows"] == {"SDAX": 1}
    assert "SYNTHETIC_PRIVATE_MARKER" not in json.dumps(status)


def test_missing_updated_is_kept_with_unknown_recency(
    storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Valid creation is sufficient for import even without update recency."""
    value = raw()
    value["fields"].pop("updated")
    monkeypatch.setattr(sync_checkpoints, "fetch_jira_issues", lambda *a, **k: [value])
    report = desk_sync.run_sync(request(projects=["SDAX"]))
    assert report.counts == {"SDAX": 1} and report.complete
    result = pd.read_pickle(storage).iloc[0]
    assert pd.isna(result.updated)
    assert pd.isna(result.business_days_created_to_updated)


def test_live_fetch_reports_invalid_dates_once(
    storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Actual page validation reports rejections once through the service."""
    from unittest.mock import MagicMock

    good, bad = raw(), raw(number=2)
    bad["fields"]["created"] = None
    client = MagicMock()
    client.myself.return_value = {"timeZone": "UTC"}
    client.enhanced_search_issues.return_value = {"issues": [bad, good]}
    monkeypatch.setattr(jira_loader, "JIRA", lambda **kwargs: client)
    monkeypatch.setenv("JIRA_URL", "https://jira.example")
    monkeypatch.setenv("JIRA_USERNAME", "test-user")
    monkeypatch.setenv("JIRA_PASSWORD", "synthetic-password")
    report = desk_sync.run_sync(request(projects=["SDAX"]))
    assert report.counts == {"SDAX": 1}
    assert report.invalid_rows == {"SDAX": 1}
    assert not report.complete and report.committed
