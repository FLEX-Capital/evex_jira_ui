"""Conservative retention recognizes only owned artifacts in synthetic roots."""

import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import data_loading
from settings import DataPaths, load_settings

NOW = datetime(2026, 10, 20, 12, tzinfo=UTC)


def _paths(root: Path) -> DataPaths:
    return load_settings({"DATA_DIR": str(root)}).paths


def _temp(target: Path, age: timedelta, *, name: str = "abcdefgh") -> Path:
    path = target.with_name(f".evex-atomic-{target.name}-{name}.tmp")
    path.write_bytes(b"synthetic")
    when = (NOW - age).timestamp()
    os.utime(path, (when, when))
    return path


def test_dry_run_has_no_candidates_in_an_empty_root(tmp_path: Path) -> None:
    """Inspection must not create even an absent DATA_DIR or lock sidecar."""
    from cache_maintenance import plan_cleanup

    root = tmp_path / "absent"
    plan = plan_cleanup(_paths(root), now=NOW)
    assert plan.candidates == () and len(plan.digest) == 64
    assert not root.exists()


def test_retention_is_union_of_newest_ten_and_fourteen_days(tmp_path: Path) -> None:
    """Top ten by encoded UTC time and every recent/boundary backup are kept."""
    from cache_maintenance import apply_cleanup, plan_cleanup

    paths = _paths(tmp_path)
    backups = []
    for days in [*range(20, 32), 14, 3]:
        timestamp = NOW - timedelta(days=days)
        path = tmp_path / f"{paths.tickets.name}.bak-import-{timestamp:%Y%m%d-%H%M%S-%f}"
        path.write_bytes(b"synthetic")
        backups.append(path)
    # Filesystem mtime is deliberately unrelated to the encoded backup time.
    plan = plan_cleanup(paths, now=NOW)
    expected = set(backups[8:12])
    assert {item.path for item in plan.candidates} == expected
    assert all(item.family == "ticket" for item in plan.candidates)
    assert set(apply_cleanup(plan, paths, now=NOW)) == expected
    assert all(path.exists() == (path not in expected) for path in backups)


@pytest.mark.parametrize("change", ["digest", "new_candidate", "new_inode", "active_import"])
def test_dry_run_and_apply_digest_are_fail_closed(tmp_path: Path, change: str) -> None:
    """Any stale-at-entry plan or active import prevents every deletion."""
    from cache_maintenance import apply_cleanup, plan_cleanup

    paths = _paths(tmp_path)
    selected = _temp(paths.tickets, timedelta(days=2))
    before = selected.read_bytes()
    plan = plan_cleanup(paths, now=NOW)
    assert selected.read_bytes() == before
    if change == "digest":
        plan = replace(plan, digest="0" * 64)
    elif change == "new_candidate":
        _temp(paths.countries, timedelta(days=2))
    elif change == "new_inode":
        temporary = tmp_path / "replacement"
        temporary.write_bytes(before)
        temporary.replace(selected)
    if change == "active_import":
        with data_loading.import_operation_lock(paths.tickets):
            with pytest.raises(data_loading.ImportBusyError):
                apply_cleanup(plan, paths, now=NOW)
    else:
        with pytest.raises(data_loading.CacheChangedError):
            apply_cleanup(plan, paths, now=NOW)
    assert selected.exists() and selected.read_bytes() == before


def test_partial_logs_unrelated_files_and_symlinks_are_protected(
    tmp_path: Path,
) -> None:
    """No recursive, broad suffix or symlink cleanup can touch recovery/history."""
    from cache_maintenance import apply_cleanup, plan_cleanup

    root = tmp_path / "data"
    root.mkdir()
    paths = _paths(root)
    names = [
        "jira_issues.json",
        "jira_issues.meta.json",
        "import_status.json",
        "backfill_SDAX.json",
        "backfill_SDAX.partial.json",
        "backfill_SDAX.partial.ndjson",
        "unrelated.tmp",
        "jira_data.pkl.bak-not-a-time",
        "jira_data.pkl.lock",
        ".evex-atomic-unrelated.json-abcdefgh.tmp",
    ]
    protected = []
    for name in names:
        path = root / name
        path.write_bytes(b"protected")
        os.utime(path, (0, 0))
        protected.append(path)
    directory = root / "nested"
    directory.mkdir()
    nested = _temp(directory / paths.tickets.name, timedelta(days=3))
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside")
    symlink = root / ".evex-atomic-jira_data.pkl-foreign1.tmp"
    symlink.symlink_to(outside)
    eligible = _temp(paths.tickets, timedelta(days=3))
    plan = plan_cleanup(paths, now=NOW)
    assert [candidate.path for candidate in plan.candidates] == [eligible]
    assert set(protected + [directory, symlink]) <= set(plan.protected)
    assert apply_cleanup(plan, paths, now=NOW) == (eligible,)
    assert all(path.read_bytes() == b"protected" for path in protected)
    assert nested.exists() and outside.read_bytes() == b"outside" and symlink.is_symlink()


def test_cleanup_uses_owner_lock_order_and_temp_age(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Family transactions stay separate; exact 24-hour temporaries stay protected."""
    import cache_maintenance as maintenance

    paths = _paths(tmp_path)
    boundary = _temp(paths.tickets, timedelta(hours=24), name="boundary")
    selected = [
        _temp(target, timedelta(hours=24, microseconds=1))
        for target in (paths.tickets, paths.countries, paths.classifications)
    ]
    events = []
    active: list[str] = []

    @contextmanager
    def operation(path: Path) -> Iterator[None]:
        assert not active
        active.append("operation")
        events.append("operation")
        try:
            yield
        finally:
            active.pop()

    @contextmanager
    def ticket(path: Path, **kwargs: Any) -> Iterator[None]:
        assert active == ["operation"]
        active.append("ticket")
        events.append("ticket")
        try:
            yield
        finally:
            active.pop()

    @contextmanager
    def family(path: Path, **kwargs: Any) -> Iterator[None]:
        assert not active
        active.append(path.name)
        events.append(path.name)
        try:
            yield
        finally:
            active.pop()

    monkeypatch.setattr(maintenance, "import_operation_lock", operation)
    monkeypatch.setattr(maintenance, "cache_lock", ticket)
    monkeypatch.setattr(maintenance, "path_lock", family)
    plan = maintenance.plan_cleanup(paths, now=NOW)
    assert {item.path for item in plan.candidates} == set(selected)
    assert set(maintenance.apply_cleanup(plan, paths, now=NOW)) == set(selected)
    assert events[:4] == [
        "operation",
        "ticket",
        paths.countries.name,
        paths.classifications.name,
    ]
    assert boundary.exists() and not active


def test_busy_family_prevents_any_deletion(tmp_path: Path) -> None:
    """Preflight refuses an active country writer before pruning a ticket temp."""
    from cache_maintenance import apply_cleanup, plan_cleanup
    from file_io import path_lock

    paths = _paths(tmp_path)
    selected = [_temp(target, timedelta(days=2)) for target in (paths.tickets, paths.countries)]
    entered, release = threading.Event(), threading.Event()

    def writer() -> None:
        with path_lock(paths.countries):
            entered.set()
            assert release.wait(10)

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        assert entered.wait(3)
        plan = plan_cleanup(paths, now=NOW)
        with pytest.raises(BlockingIOError):
            apply_cleanup(plan, paths, now=NOW)
        assert all(path.exists() for path in selected)
    finally:
        release.set()
        thread.join(3)
    assert not thread.is_alive()


def test_cleanup_reports_exact_partial_unlink_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A partial I/O failure reports both completed and failed safe paths."""
    import cache_maintenance as maintenance

    paths = _paths(tmp_path)
    one = _temp(paths.tickets, timedelta(days=2), name="aaaaaaaa")
    two = _temp(paths.tickets, timedelta(days=2), name="bbbbbbbb")
    original = Path.unlink

    def unlink(path: Path, *args: Any, **kwargs: Any) -> None:
        if path == two:
            raise OSError("SYNTHETIC_PRIVATE_MARKER")
        original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    with pytest.raises(maintenance.CleanupError) as caught:
        maintenance.apply_cleanup(maintenance.plan_cleanup(paths, now=NOW), paths, now=NOW)
    assert caught.value.deleted == (one,) and caught.value.failed == (two,)
    assert "SYNTHETIC_PRIVATE_MARKER" not in str(caught.value)
    assert not one.exists() and two.exists()
    plan = maintenance.plan_cleanup(paths, now=datetime.now(UTC))
    assert (
        maintenance.main(
            [
                "cleanup",
                "--data-dir",
                str(tmp_path),
                "--apply",
                "--plan-digest",
                plan.digest,
            ]
        )
        == 3
    )
    output = capsys.readouterr().out
    assert str(two) in output and "SYNTHETIC_PRIVATE_MARKER" not in output


def test_cleanup_cli_requires_the_reviewed_digest(tmp_path: Path) -> None:
    """The CLI defaults to inspection and applies only the matching plan."""
    from cache_maintenance import main, plan_cleanup

    paths = _paths(tmp_path)
    selected = _temp(paths.tickets, timedelta(days=3))
    assert main(["cleanup", "--data-dir", str(tmp_path)]) == 0
    assert selected.exists()
    assert main(["cleanup", "--data-dir", str(tmp_path), "--apply"]) == 2
    plan = plan_cleanup(paths, now=datetime.now(UTC))
    assert (
        main(
            [
                "cleanup",
                "--data-dir",
                str(tmp_path),
                "--apply",
                "--plan-digest",
                plan.digest,
            ]
        )
        == 0
    )
    assert not selected.exists()
