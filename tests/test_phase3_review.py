"""Independent-review regressions for legacy data and partial cleanup results."""

import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

import cache_maintenance as maintenance
import data_loading
from cache_schema import SCHEMA_ATTR, CacheSchemaError, normalize_frame
from settings import load_settings


def test_empty_legacy_columns_receive_canonical_dtypes(tmp_path: Path) -> None:
    """Existing empty object columns cannot make an empty dataset unreadable."""
    old = pd.DataFrame(columns=["key", "created", "updated", "is_done", "custom"])
    old.attrs["history"] = "keep"
    result = normalize_frame(old)
    assert result.empty and result.attrs[SCHEMA_ATTR] == 2
    assert result.attrs["history"] == "keep" and "custom" in result
    assert str(result.created.dtype) == "datetime64[ns, Europe/Berlin]"
    assert str(result.is_done.dtype) == "boolean"
    assert old.created.dtype == object and SCHEMA_ATTR not in old.attrs
    path = tmp_path / "jira_data.pkl"
    old.to_pickle(path)
    before = path.read_bytes()
    assert data_loading.load_data(path=path).empty
    assert maintenance.main(["migrate", "--path", str(path)]) == 0
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "column,value", [("has_exax_clone", "SYNTHETIC_PRIVATE_MARKER"), ("n_clones", 1.5)]
)
def test_malformed_legacy_values_have_safe_loader_cli_and_ui_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    public_ui: None,
    column: str,
    value: object,
) -> None:
    """Expected coercion errors are schema failures, never raw type exceptions."""
    frame = pd.DataFrame([{"key": "SDAX-1", column: value}])
    with pytest.raises(CacheSchemaError) as caught:
        normalize_frame(frame)
    assert caught.value.columns == (column,)
    assert "SYNTHETIC_PRIVATE_MARKER" not in str(caught.value)
    path = tmp_path / "jira_data.pkl"
    frame.to_pickle(path)
    before = path.read_bytes()
    with pytest.raises(CacheSchemaError):
        data_loading.load_data(path=path)
    assert maintenance.main(["migrate", "--path", str(path)]) == 2
    monkeypatch.setitem(sys.modules, "__main__", sys.modules["__main__"])
    app = AppTest.from_file("app.py", default_timeout=30).run()
    assert not app.exception and app.error
    assert "SYNTHETIC_PRIVATE_MARKER" not in app.error[0].value
    assert path.read_bytes() == before


@pytest.mark.parametrize("entry_point", ["api", "cli"])
def test_partial_cleanup_inventory_io_failure_preserves_exact_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    entry_point: str,
) -> None:
    """A vanished other-family temp must not erase completed deletion evidence."""
    paths = load_settings().paths
    now = datetime.now(UTC)
    selected = []
    for target in (paths.tickets, paths.countries):
        path = target.with_name(f".evex-atomic-{target.name}-abcdefgh.tmp")
        path.write_bytes(b"synthetic")
        timestamp = (now - timedelta(days=2)).timestamp()
        os.utime(path, (timestamp, timestamp))
        selected.append(path)
    ticket, country = selected
    plan = maintenance.plan_cleanup(paths, now=now)
    original_inventory = maintenance._inventory

    def inventory(root: Path) -> tuple[maintenance.Inventory, tuple[Path, ...]]:
        if not ticket.exists():
            raise FileNotFoundError("SYNTHETIC_PRIVATE_MARKER")
        return original_inventory(root)

    monkeypatch.setattr(maintenance, "_inventory", inventory)
    if entry_point == "api":
        with pytest.raises(maintenance.CleanupError) as caught:
            maintenance.apply_cleanup(plan, paths, now=now)
        assert caught.value.deleted == (ticket,) and caught.value.failed == (country,)
        assert "SYNTHETIC_PRIVATE_MARKER" not in str(caught.value)
    else:
        assert (
            maintenance.main(["cleanup", "--apply", "--plan-digest", plan.digest]) == 3
        )
        output = capsys.readouterr().out
        assert str(ticket) in output and str(country) in output
        assert "SYNTHETIC_PRIVATE_MARKER" not in output
    assert not ticket.exists() and country.exists()
