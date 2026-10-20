"""All mutable defaults follow one captured root, independent of process cwd."""

import json
import os
import subprocess
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock, patch

import pandas as pd
import pytest

from settings import load_settings

ROOT = Path(__file__).resolve().parents[1]


def test_relative_data_root_is_project_relative_from_any_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Relative DATA_DIR belongs to the project, while assets stay static."""
    monkeypatch.chdir(tmp_path)
    project = tmp_path / "project"
    paths = load_settings({"DATA_DIR": "var/cache"}, project_root=project).paths
    assert paths.tickets == project / "var/cache/jira_data.pkl"
    assert paths.logo == project / "assets/evex_logo.png"
    assert paths.category_map == project / "assets/object_id_to_name.json"
    assert not paths.root.exists()


def test_every_default_artifact_and_sidecar_shares_the_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default cache families, raw export and locks cannot split across roots."""
    import asset_country
    import data_loading
    import jira_loader
    import ticket_classifier
    from sync_checkpoints import CheckpointStore

    root = tmp_path / "configured"
    cwd = tmp_path / "other"
    cwd.mkdir()
    monkeypatch.setenv("DATA_DIR", str(root))
    monkeypatch.chdir(cwd)
    paths = load_settings().paths
    frame = pd.DataFrame([{"key": "SDAX-1"}])
    data_loading.save_data(frame)
    assert paths.tickets.exists()
    with data_loading.import_operation_lock():
        pass
    backup = data_loading.save_data_if_unchanged(
        frame, data_loading.cache_revision(), backup_prefix="test"
    )
    assert backup is not None and Path(backup).parent == root
    assert Path(f"{paths.tickets}.lock").exists()
    assert Path(f"{paths.tickets}.import.lock").exists()
    asset_country.save_cache({"1": "Deutschland"})
    assert paths.countries.exists() and asset_country.load_cache() == {
        "1": "Deutschland"
    }
    ticket_classifier.save_cache(frame)
    assert paths.classifications.exists()
    client = Mock()
    client.myself.return_value = {"timeZone": "UTC"}
    client.enhanced_search_issues.return_value = {"issues": []}
    with (
        patch.object(jira_loader, "JIRA", return_value=client),
        patch.dict(
            os.environ,
            {
                "JIRA_URL": "https://jira.example",
                "JIRA_USERNAME": "test-user",
                "JIRA_PASSWORD": "synthetic-password",
            },
        ),
    ):
        jira_loader.fetch_jira_issues(project="SDAX")
    assert json.loads(paths.raw_snapshot.read_text()) == []
    store = CheckpointStore()
    store.fetch_jira_issues = Mock(return_value=[])
    store.fetch_project(
        "SDAX",
        datetime(2026, 9, 1, tzinfo=UTC),
        datetime(2026, 9, 2, tzinfo=UTC),
        False,
    )
    assert paths.checkpoint("SDAX").exists()
    assert not (cwd / "data").exists()


def test_explicit_alternate_ticket_path_freezes_worker_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A background import keeps its admitted paths across environment changes."""
    from test_sync_service import raw

    import sync_checkpoints
    from desk_sync import make_sync_request
    from sync_jobs import SyncJobs

    entered, release = threading.Event(), threading.Event()

    def fetch(*args: object, **kwargs: object) -> list[dict[str, object]]:
        entered.set()
        assert release.wait(5)
        return [raw()]

    first, later = tmp_path / "chosen", tmp_path / "later"
    alternate = first / "custom.pkl"
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "initial-default"))
    monkeypatch.setattr(sync_checkpoints, "fetch_jira_issues", fetch)
    jobs = SyncJobs()
    request = make_sync_request(
        datetime(2026, 9, 1, tzinfo=UTC),
        datetime(2026, 9, 2, tzinfo=UTC),
        ["SDAX"],
        publish_raw=True,
    )
    try:
        assert jobs.start(request, data_path=alternate)
        assert entered.wait(5)
        monkeypatch.setenv("DATA_DIR", str(later))
    finally:
        release.set()
        if jobs.thread:
            jobs.thread.join(10)
    assert jobs.thread is not None and not jobs.thread.is_alive()
    assert jobs.snapshot()["report"]["complete"]
    assert alternate.exists()
    for name in [
        "import_status.json",
        "backfill_SDAX.json",
        "jira_issues.json",
        "jira_issues.meta.json",
    ]:
        assert (first / name).exists()
    assert not later.exists()


def test_path_defaults_do_not_create_directories_during_import(tmp_path: Path) -> None:
    """Support modules import from a foreign cwd without touching data storage."""
    root = tmp_path / "uncreated"
    env = dict(
        os.environ,
        DATA_DIR=str(root),
        PYTHONPATH=str(ROOT),
        PYTHONDONTWRITEBYTECODE="1",
    )
    script = (
        "import data_loading,data_transformation,jira_loader,asset_country,"
        "ticket_classifier,sync_jobs,backfill_jira,backfill_country,"
        "asset_migration,jira_country_export;print('ok')"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
    assert not root.exists()
