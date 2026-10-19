from datetime import UTC, datetime
from unittest.mock import Mock, patch

import pytest

import backfill_jira as backfill
import data_loading
import desk_sync
import jira_loader
import sync_checkpoints
from category_sync import CategoryRepairReport


@pytest.fixture(autouse=True)
def isolated_cli(tmp_path, monkeypatch):
    monkeypatch.setattr(data_loading, "DATA_PATH", str(tmp_path / "cache.pkl"))
    monkeypatch.setattr(
        backfill, "DATA_PATH", str(tmp_path / "cache.pkl"), raising=False
    )
    monkeypatch.setattr(
        backfill, "CHECKPOINT_TMPL", str(tmp_path / "{project}.json"), raising=False
    )

    def forbidden(*a, **k):
        raise AssertionError("A test attempted an unmocked Jira connection")

    monkeypatch.setattr(jira_loader, "JIRA", forbidden)


def test_cli_maps_date_and_project_options_to_common_service():
    runner = Mock(return_value=desk_sync.SyncReport())
    with patch.object(backfill, "run_sync", runner):
        assert (
            backfill.main(
                [
                    "--project",
                    "SDAX",
                    "--project",
                    "SDEU",
                    "--start",
                    "2025-01-01",
                    "--end",
                    "2025-03-17",
                    "--resume",
                ]
            )
            == 0
        )
    request = runner.call_args.args[0]
    assert request.projects == ("SDAX", "SDEU") and request.resume
    assert request.start == datetime(2024, 12, 31, 23, tzinfo=UTC)
    assert request.end == datetime(2025, 3, 17, 22, 59, 59, 999999, tzinfo=UTC)
    assert not request.repair_sources and not request.publish_raw


@pytest.mark.parametrize("flags,days", [([], 365), (["--days", "60"], 60)])
def test_cli_default_and_relative_windows_use_captured_now(flags, days):
    runner = Mock(return_value=desk_sync.SyncReport())
    with (
        patch.object(backfill, "run_sync", runner),
        patch.object(backfill, "datetime", wraps=datetime) as clock,
    ):
        clock.now.return_value = datetime(2026, 9, 10, tzinfo=UTC)
        assert backfill.main(flags) == 0
    request = runner.call_args.args[0]
    assert (request.end - request.start).days == days
    assert request.projects == ("SDIPR", "SDAX", "SDEU")


def test_cli_unresolved_categories_and_busy_import_return_nonzero(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(data_loading, "DATA_PATH", str(tmp_path / "cache.pkl"))
    report = desk_sync.SyncReport(categories=CategoryRepairReport(unresolved_cells=1))
    with patch.object(backfill, "run_sync", return_value=report):
        assert backfill.main(["--days", "7"]) == 1
    with data_loading.import_operation_lock():
        assert backfill.main(["--days", "7"]) == 1


def test_cli_runs_real_core_without_a_browser_or_writes_outside_its_storage(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(data_loading, "DATA_PATH", str(tmp_path / "cache.pkl"))
    monkeypatch.setattr(sync_checkpoints, "fetch_jira_issues", lambda *a, **k: [])
    assert backfill.main(["--project", "SDAX", "--days", "7"]) == 0
    assert (tmp_path / "import_status.json").exists()
    assert (tmp_path / "backfill_SDAX.json").exists()


def test_invalid_windows_and_conflicting_flags_never_start_import():
    with patch.object(backfill, "run_sync") as runner:
        for flags in (
            ["--start", "2026-09-01", "--days", "7"],
            ["--days", "0"],
            ["--start", "2026-10-01", "--end", "2026-09-01"],
        ):
            with pytest.raises(SystemExit):
                backfill.main(flags)
        assert not runner.called
