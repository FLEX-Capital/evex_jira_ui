"""Maintenance owns full-cache transactions and revalidates each action's session."""

import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import test_ui
from streamlit.testing.v1 import AppTest

from data_loading import CacheChangedError, load_data, save_data
from settings import load_settings


def _assert_ticket_lock_free(path: Path) -> None:
    code = (
        "from pathlib import Path\nfrom file_io import path_lock\nimport sys\n"
        "with path_lock(Path(sys.argv[1]), blocking=False):\n    print('free')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code, str(path)],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0 and result.stdout.strip() == "free"


def test_maintenance_uses_full_cache_not_filtered_view(
    public_ui: None,
    tmp_path: Path,
) -> None:
    """Both helpers include old hidden tickets while keeping input/display state separate."""
    import maintenance_ui as maintenance

    frame = test_ui.CountryRefreshUITests()._frame().assign(source="")
    path = tmp_path / "jira_data.pkl"
    save_data(frame, path=path)
    received: list[str] = []

    def sources(keys: list[str], **kwargs: Any) -> dict[str, str]:
        _assert_ticket_lock_free(path)
        received.extend(keys)
        return dict.fromkeys(keys, "synthetic source")

    with patch.object(maintenance, "fetch_issue_sources", side_effect=sources):
        source = maintenance.refresh_source_cache(path)
    assert source.kind == "source" and source.changed == 2 and not source.incomplete
    assert set(received) == set(frame.key)
    assert set(load_data(path=path).source) == {"synthetic source"}
    with patch(
        "asset_country.fetch_country_asset",
        side_effect=lambda identity, **kw: {
            "id": identity.object_id,
            "attributes": [
                {
                    "objectTypeAttribute": {"name": "Land"},
                    "objectAttributeValues": [{"displayValue": "Österreich"}],
                }
            ],
        },
    ):
        country = maintenance.refresh_country_cache(path, force=True)
    assert country.kind == "country" and country.changed == 2
    assert set(load_data(path=path).Land) == {"Österreich"}
    assert frame.source.eq("").all() and frame.created.min().year == 2020


def test_concurrent_refresh_preserves_new_ticket_snapshot_and_country_work(
    public_ui: None,
    tmp_path: Path,
) -> None:
    """Resolved country entries survive a refused stale ticket write and newer fields survive."""
    import maintenance_ui as maintenance
    from asset_country import load_cache

    frame = test_ui.CountryRefreshUITests()._frame().assign(source="nonblank source")
    path = tmp_path / "jira_data.pkl"
    save_data(frame, path=path)

    def fetch(identity: Any, **kwargs: Any) -> dict[str, Any]:
        _assert_ticket_lock_free(path)
        save_data(frame.assign(Hauptkategorie="newer category"), path=path)
        return {
            "id": identity.object_id,
            "attributes": [
                {
                    "objectTypeAttribute": {"name": "Land"},
                    "objectAttributeValues": [{"displayValue": "Österreich"}],
                }
            ],
        }

    with patch("asset_country.fetch_country_asset", side_effect=fetch):
        with pytest.raises(CacheChangedError):
            maintenance.refresh_country_cache(path, force=True)
    current = load_data(path=path)
    assert current.Hauptkategorie.eq("newer category").all()
    assert current.source.eq("nonblank source").all()
    assert load_cache(path=load_settings().paths.countries).entries


@pytest.mark.parametrize("kind", ["source", "country"])
def test_stale_session_cannot_read_or_start_maintenance(
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    """Rotating after the page gate but before the action forces login before any cache read."""
    import maintenance_ui as maintenance

    monkeypatch.setenv("UI_PASSWORD", "A")
    script = f"""import os
import streamlit as st
from ui_auth import require_password
from maintenance_ui import refresh_{kind}_cache
from settings import load_settings
require_password()
if st.button("Rotate", key="rotate"):
    os.environ["UI_PASSWORD"] = "B"
    refresh_{kind}_cache(load_settings().paths.tickets)
"""
    with (
        patch.object(maintenance, "load_data_snapshot") as read,
        patch.object(maintenance, "save_data_if_unchanged") as write,
    ):
        app = AppTest.from_string(script).run()
        app.sidebar.text_input[0].set_value("A").run()
        app.button(key="rotate").click().run()
        app.run()
    assert not app.exception and app.sidebar.text_input and not app.button
    assert not read.called and not write.called


def test_failed_optional_maintenance_has_safe_actionable_feedback(
    public_ui: None,
) -> None:
    """Errors show only bounded cause/stage/run identity and never claim success."""
    import maintenance_ui as maintenance

    with patch.object(
        maintenance,
        "refresh_source_cache",
        side_effect=PermissionError("PRIVATE_MARKER"),
    ):
        app = AppTest.from_string(
            "from maintenance_ui import render_maintenance\nfrom settings import load_settings\n"
            "render_maintenance(load_settings())"
        ).run()
        next(
            button for button in app.button if "Ursprung" in button.label
        ).click().run()
    assert not app.exception and not app.success
    assert any("permission" in error.value for error in app.error)
    rendered = " ".join(element.value for element in [*app.error, *app.caption])
    assert "source" in rendered and "PRIVATE_MARKER" not in rendered
    diagnostic = app.session_state["maintenance_diagnostic"]
    assert diagnostic["operation_id"] in rendered and diagnostic["code"] == "permission"


def test_empty_state_keeps_custom_import_controls(public_ui: None) -> None:
    """An empty dashboard still exposes independent custom desk/date import choices."""
    from maintenance_ui import render_import_empty_state

    assert callable(render_import_empty_state)
    app = AppTest.from_string(
        "from maintenance_ui import render_import_empty_state\n"
        "render_import_empty_state('No data')"
    ).run()
    assert not app.exception and any(info.value == "No data" for info in app.info)
    assert app.date_input(key="temporary_history_dates")
    assert app.button(key="temporary_history_start")
    app.date_input(key="temporary_history_dates").set_value(
        (date(2020, 1, 1), date(2020, 1, 2))
    ).run()
    app.multiselect(key="temporary_history_desks").set_value(["SDEU"]).run()
    assert app.multiselect(key="temporary_history_desks").value == ["SDEU"]
    assert app.date_input(key="temporary_history_dates").value == (
        date(2020, 1, 1),
        date(2020, 1, 2),
    )


def test_incomplete_country_outcome_shows_counts_and_safe_cause(
    public_ui: None,
) -> None:
    """Partial work retains resolved/changed counts without hiding failed or pending work."""
    import maintenance_ui as maintenance
    from operation_errors import OperationDiagnostic

    outcome = maintenance.MaintenanceOutcome(
        "country",
        4,
        True,
        (OperationDiagnostic("synthetic-run", "assets", "connection"),),
        counts={"resolved": 1, "failed": 2, "pending": 3},
    )
    with patch.object(maintenance, "refresh_country_cache", return_value=outcome):
        app = AppTest.from_string(
            "from maintenance_ui import render_maintenance\nfrom settings import load_settings\n"
            "render_maintenance(load_settings())"
        ).run()
        next(button for button in app.button if "Länder" in button.label).click().run()
    assert not app.exception
    warnings = " ".join(warning.value for warning in app.warning)
    assert "2 Asset" in warnings and "3 Tickets" in warnings
    assert any(
        "1 Assets" in success.value and "4 Tickets" in success.value
        for success in app.success
    )
    assert any(
        "synthetic-run" in caption.value and "connection" in caption.value
        for caption in app.caption
    )
