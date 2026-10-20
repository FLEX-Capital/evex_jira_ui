"""Ticket schema normalization is pure, explicit and safe at the UI boundary."""

import pickle  # nosec B403
import sys
from pathlib import Path

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest
from test_sync_service import raw

import data_loading
from data_transformation import load_project_issues, upsert_jira_data
from settings import TENANT_ATTR


def test_legacy_normalization_is_pure_and_preserves_unknown_columns() -> None:
    """Minimal history keeps all supplied values and unrelated metadata."""
    from cache_schema import SCHEMA_ATTR, normalize_frame

    old = pd.DataFrame(
        [
            {
                "key": "SDAX-1",
                "summary": "synthetic",
                "custom": 7,
                "Land": "Österreich",
                "clones": "EXAX-1",
            }
        ]
    )
    old.attrs = {
        "custom_metadata": {"owner": "synthetic"},
        TENANT_ATTR: {"example": "kept"},
    }
    before = old.copy(deep=True)
    new = normalize_frame(old)
    assert new.attrs[SCHEMA_ATTR] == 2
    assert new.attrs[TENANT_ATTR] == old.attrs[TENANT_ATTR]
    assert new.attrs["custom_metadata"] == old.attrs["custom_metadata"]
    assert new.loc[0, "custom"] == 7 and new.loc[0, "Land"] == "Österreich"
    assert new.loc[0, "completion_state"] == "unknown"
    assert pd.isna(new.loc[0, "clone_count"])
    assert pd.isna(new.loc[0, "issue_links"])
    pd.testing.assert_frame_equal(old, before)
    pd.testing.assert_frame_equal(new, normalize_frame(new))


def test_cache_states_have_distinct_results(tmp_path: Path) -> None:
    """Missing, legacy, corrupt, non-frame and future artifacts stay distinguishable."""
    from cache_schema import SCHEMA_ATTR, CacheReadError, CacheSchemaError

    target = tmp_path / "jira_data.pkl"
    assert data_loading.load_data(path=target) is None
    old = pd.DataFrame([{"key": "SDAX-1"}])
    old.to_pickle(target)
    before = target.read_bytes()
    assert data_loading.load_data(path=target).attrs[SCHEMA_ATTR] == 2
    assert target.read_bytes() == before
    for invalid in (b"SYNTHETIC_PRIVATE_MARKER", pickle.dumps({"private": "value"})):
        target.write_bytes(invalid)
        with pytest.raises(CacheReadError) as caught:
            data_loading.load_data(path=target)
        assert "PRIVATE" not in str(caught.value) and "value" not in str(caught.value)
    old.attrs[SCHEMA_ATTR] = 99
    old.to_pickle(target)
    with pytest.raises(CacheSchemaError, match="version"):
        data_loading.load_data(path=target)


@pytest.mark.parametrize("version", [None, True, False, 0, -1, "2", 2.0, [], {}])
def test_invalid_version_is_rejected(version: object) -> None:
    """Schema versions are explicit supported integers, never coercible hints."""
    from cache_schema import SCHEMA_ATTR, CacheSchemaError, normalize_frame

    frame = pd.DataFrame([{"key": "SDAX-1"}])
    frame.attrs[SCHEMA_ATTR] = version
    with pytest.raises(CacheSchemaError, match="version"):
        normalize_frame(frame)


def test_version_one_is_normalized_without_mutating_its_stamp() -> None:
    """The explicitly supported older version follows the same pure upgrade."""
    from cache_schema import SCHEMA_ATTR, normalize_frame

    old = pd.DataFrame([{"key": "SDAX-1"}])
    old.attrs[SCHEMA_ATTR] = 1
    assert normalize_frame(old).attrs[SCHEMA_ATTR] == 2
    assert old.attrs[SCHEMA_ATTR] == 1


@pytest.mark.parametrize(
    "defect",
    [
        "missing_column",
        "bad_boolean",
        "bad_date",
        "bad_links",
        "bad_link_items",
        "inconsistent_state",
        "duplicate_key",
    ],
)
def test_asserted_v2_malformed_records_are_rejected(defect: str) -> None:
    """A v2 stamp does not excuse missing columns, invalid types or duplicates."""
    from cache_schema import CacheSchemaError, normalize_frame

    frame = normalize_frame(load_project_issues("SDAX", [raw()]))
    if defect == "missing_column":
        frame = frame.drop(columns=["created"])
    elif defect == "bad_boolean":
        frame["is_done"] = "yes"
    elif defect == "bad_date":
        frame["created"] = "SYNTHETIC_PRIVATE_MARKER"
    elif defect == "bad_links":
        frame["issue_links"] = "not a list"
    elif defect == "bad_link_items":
        frame.at[0, "issue_links"] = ["not a normalized link"]
    elif defect == "inconsistent_state":
        frame["is_done"] = False
    else:
        frame = pd.concat([frame, frame], ignore_index=True)
    with pytest.raises(CacheSchemaError):
        normalize_frame(frame)


def test_legacy_duplicate_recency_and_nullable_types() -> None:
    """UTC recency and last-input ties win; bad dates and unknown flags survive."""
    from cache_schema import normalize_frame

    frame = pd.DataFrame(
        [
            {"key": "SDAX-1", "summary": "old", "updated": "2026-09-01T12:00:00+02:00"},
            {"key": "SDAX-1", "summary": "new", "updated": "2026-09-01T11:00:00Z"},
            {"key": "SDAX-1", "summary": "tie", "updated": "2026-09-01T13:00:00+02:00"},
            {"key": "SDEU-2", "created": "bad", "has_exax_clone": pd.NA},
        ]
    )
    result = normalize_frame(frame)
    assert result.key.tolist() == ["SDAX-1", "SDEU-2"]
    assert result.loc[0, "summary"] == "tie"
    assert str(result.created.dtype) == "datetime64[ns, Europe/Berlin]"
    assert result.created.isna().all() and result.duration_quality.eq("missing").all()
    assert str(result.is_done.dtype) == "boolean" and result.is_done.isna().all()
    assert pd.isna(result.loc[1, "has_exax_clone"])
    assert result.clone_count.isna().all()
    empty = normalize_frame(pd.DataFrame())
    assert set(empty) == set(normalize_frame(pd.DataFrame([{"key": "SDAX-1"}])))
    assert str(empty.created.dtype) == "datetime64[ns, Europe/Berlin]"


@pytest.mark.parametrize("key", [None, "", "   ", 42])
def test_nonempty_cache_requires_valid_ticket_keys(key: object) -> None:
    """Invalid records cannot quietly disappear from retained history."""
    from cache_schema import CacheSchemaError, normalize_frame

    with pytest.raises(CacheSchemaError, match="key"):
        normalize_frame(pd.DataFrame([{"key": key}]))


def test_merge_save_keeps_schema_and_concurrent_fields(tmp_path: Path) -> None:
    """Narrow patches and raw refresh retain schema, unrelated attrs and fields."""
    from cache_schema import SCHEMA_ATTR

    path = tmp_path / "jira_data.pkl"
    original = load_project_issues("SDAX", [raw()])
    original.attrs["history"] = "keep"
    original["operator_note"] = "keep"
    original["Land"] = "Österreich"
    data_loading.save_data(original, path=path)
    current, revision = data_loading.load_data_snapshot(path)
    current.loc[0, "source"] = "Telefon"
    data_loading.save_data_if_unchanged(current, revision, path=path)
    fresh = load_project_issues("SDAX", [raw(number=2)])
    merged = upsert_jira_data(data_loading.load_data(path=path), fresh)
    data_loading.save_data(merged, path=path)
    result = data_loading.load_data(path=path)
    assert result.attrs[SCHEMA_ATTR] == 2 and result.attrs["history"] == "keep"
    old = result.set_index("key").loc["SDAX-1"]
    assert old["Land"] == "Österreich" and old["source"] == "Telefon"
    assert old["operator_note"] == "keep"


def test_corrupt_cache_shows_actionable_error_after_authentication(
    tmp_path: Path, public_ui: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Corruption is reported safely instead of appearing as an empty dashboard."""
    (tmp_path / "jira_data.pkl").write_bytes(b"SYNTHETIC_PRIVATE_MARKER")
    # AppTest installs its script as __main__; restore it before process-spawn
    # tests, which otherwise try to execute the dashboard as their entry point.
    monkeypatch.setitem(sys.modules, "__main__", sys.modules["__main__"])
    app = AppTest.from_file("app.py", default_timeout=30).run()
    assert not app.exception
    assert app.error and "Cache" in app.error[0].value
    assert "SYNTHETIC_PRIVATE_MARKER" not in app.error[0].value
    assert not any("Keine Jira-Daten" in message.value for message in app.info)
