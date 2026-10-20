"""Revision-guarded migration is inspectable and only mutates synthetic storage."""

from pathlib import Path

import pandas as pd
import pytest

import data_loading
from cache_schema import SCHEMA_ATTR


def _legacy(path: Path) -> bytes:
    frame = pd.DataFrame([{"key": "SDAX-1", "summary": "synthetic", "Land": "Österreich"}])
    frame.attrs["history"] = "keep"
    frame.to_pickle(path)
    return path.read_bytes()


def test_migration_dry_run_does_not_write(tmp_path: Path) -> None:
    """Inspection reports one coherent revision without a lock, backup or temp."""
    from cache_maintenance import main, plan_migration

    path = tmp_path / "jira_data.pkl"
    before = _legacy(path)
    plan = plan_migration(path)
    assert plan.source_version is None and plan.target_version == 2 and plan.rows == 1
    assert plan.revision == data_loading.cache_revision(path)
    assert main(["migrate", "--path", str(path)]) == 0
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]


def test_apply_backs_up_exact_original_and_publishes_v2(tmp_path: Path) -> None:
    """The exact source survives and already-current application is a no-op."""
    from cache_maintenance import apply_migration, plan_migration

    path = tmp_path / "jira_data.pkl"
    original = _legacy(path)
    backup = apply_migration(plan_migration(path))
    assert backup is not None and backup.read_bytes() == original
    assert ".bak-schema-" in backup.name
    current = data_loading.load_data(path=path)
    assert current.attrs[SCHEMA_ATTR] == 2 and current.attrs["history"] == "keep"
    assert current.loc[0, "Land"] == "Österreich"
    before = path.read_bytes()
    files = set(tmp_path.iterdir())
    assert apply_migration(plan_migration(path)) is None
    assert path.read_bytes() == before and set(tmp_path.iterdir()) == files


@pytest.mark.parametrize("failure", ["stale", "backup", "fsync", "replace", "active_import"])
def test_stale_or_failed_migration_preserves_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """Staleness, active admission or any publication failure leaves current bytes."""
    import cache_maintenance as maintenance
    import file_io

    path = tmp_path / "jira_data.pkl"
    before = _legacy(path)
    plan = maintenance.plan_migration(path)
    expected = ":".join(map(str, plan.revision))
    if failure == "stale":
        data_loading.save_data(pd.DataFrame([{"key": "SDAX-2"}]), path=path)
        before = path.read_bytes()
    elif failure == "backup":

        def fail_backup(target: Path, writer: object) -> None:
            raise OSError("SYNTHETIC_PRIVATE_MARKER")

        monkeypatch.setattr(maintenance, "atomic_write", fail_backup)
    elif failure == "fsync":
        original_sync = file_io.os.fsync
        calls = 0

        def sync(fd: int) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("SYNTHETIC_PRIVATE_MARKER")
            original_sync(fd)

        monkeypatch.setattr(file_io.os, "fsync", sync)
    elif failure == "replace":
        original_replace = file_io.os.replace

        def replace(source: str, destination: Path) -> None:
            if Path(destination) == path:
                raise OSError("SYNTHETIC_PRIVATE_MARKER")
            original_replace(source, destination)

        monkeypatch.setattr(file_io.os, "replace", replace)

    args = ["migrate", "--path", str(path), "--apply", "--expected-revision", expected]
    if failure == "active_import":
        with data_loading.import_operation_lock(path):
            assert maintenance.main(args) == 2
    else:
        assert maintenance.main(args) == (2 if failure == "stale" else 3)
    assert path.read_bytes() == before
    assert not list(tmp_path.glob(".evex-atomic-*.tmp"))


def test_apply_requires_explicit_valid_revision(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No apply flag can bypass review of the exact source revision."""
    from cache_maintenance import main, plan_migration

    path = tmp_path / "jira_data.pkl"
    before = _legacy(path)
    assert main(["migrate", "--path", str(path), "--apply"]) == 2
    assert main(["migrate", "--path", str(path), "--apply", "--expected-revision", "bad"]) == 2
    assert path.read_bytes() == before
    expected = ":".join(map(str, plan_migration(path).revision))
    assert main(["migrate", "--path", str(path), "--apply", "--expected-revision", expected]) == 0
    assert "SYNTHETIC_PRIVATE_MARKER" not in capsys.readouterr().out


def test_planned_revision_comes_from_the_opened_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Atomic replacement during inspection cannot pair old rows with new stat."""
    import cache_maintenance as maintenance

    path = tmp_path / "jira_data.pkl"
    _legacy(path)
    original_revision = data_loading.cache_revision(path)
    original_normalize = maintenance.normalize_frame

    def normalize(frame: pd.DataFrame) -> pd.DataFrame:
        monkeypatch.setattr(maintenance, "normalize_frame", original_normalize)
        data_loading.save_data(pd.DataFrame([{"key": "SDAX-2"}]), path=path)
        return original_normalize(frame)

    monkeypatch.setattr(maintenance, "normalize_frame", normalize)
    plan = maintenance.plan_migration(path)
    assert plan.revision == original_revision
    with pytest.raises(data_loading.CacheChangedError):
        maintenance.apply_migration(plan)
