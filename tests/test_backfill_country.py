"""Regression tests for backfill_country: banding, backups and dry runs."""

import sys
from typing import Any

import pandas as pd
import pytest

import asset_country
import backfill_country
from backfill_country import BAND_COLUMN, add_resolution_band
from resolution_bands import BAND_UNDER_1H


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A pickle plus cache on disk, with the Jira lookup stubbed out."""
    source = tmp_path / "jira_data.pkl"
    pd.DataFrame(
        {
            "key": ["A-1", "A-2"],
            "ansprechpartner": ["ID_1", ""],
            "filiale": ["", ""],
            "zentrale": ["", ""],
            "time_to_resolution_biz_hours": [0.5, 20.0],
            "is_done": [True, True],
        }
    ).to_pickle(source)

    cache = tmp_path / "asset_country.json"
    cache.write_text('{"1": "Deutschland"}', encoding="utf-8")

    # Explicit refresh revalidates legacy values through a fake resolver.
    monkeypatch.setattr(
        backfill_country,
        "resolve_asset_country_strict",
        lambda object_id, **kw: "Deutschland",
    )
    return source, cache


def _run(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["backfill_country.py", *argv])
    backfill_country.main()


def test_backup_is_taken_when_output_is_the_input_spelled_differently(workspace, monkeypatch):
    """The backup guard must compare paths, not strings.

    main() and write_pickle_atomically() both decide "is this the file we read
    from?". If one compares raw strings and the other absolute paths, a spelling
    like ./data/jira_data.pkl takes no backup yet still overwrites the file.
    """
    source, cache = workspace
    aliased = f"{source.parent}/./{source.name}"

    _run(monkeypatch, "--input", str(source), "--output", aliased, "--cache", str(cache))

    backups = list(source.parent.glob("*.bak-*"))
    assert backups, "overwrote the input without taking a backup"


def test_dry_run_writes_neither_pickle_nor_cache(workspace, monkeypatch):
    source, cache = workspace
    before_pickle = source.read_bytes()
    before_cache = cache.read_text(encoding="utf-8")

    _run(monkeypatch, "--input", str(source), "--cache", str(cache), "--dry-run")

    assert source.read_bytes() == before_pickle
    assert cache.read_text(encoding="utf-8") == before_cache
    assert not list(source.parent.glob("*.bak-*"))


def test_country_maintenance_keeps_concurrent_ticket_changes(
    workspace: tuple,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A prepared country result cannot replace changed keys, refs or Jira fields."""
    from data_loading import CacheChangedError

    source, cache = workspace
    latest = pd.read_pickle(source)
    latest.loc[0, ["summary", "ansprechpartner", "Hauptkategorie"]] = [
        "concurrent Jira value",
        "ID_changed",
        "concurrent category",
    ]

    def resolve(object_id: str, **kwargs: Any) -> str:
        latest.to_pickle(source)
        return "Deutschland"

    monkeypatch.setattr(backfill_country, "resolve_asset_country_strict", resolve)
    with pytest.raises(CacheChangedError):
        _run(monkeypatch, "--input", str(source), "--cache", str(cache))
    current = pd.read_pickle(source).iloc[0]
    assert current.summary == "concurrent Jira value"
    assert (
        current.ansprechpartner == "ID_changed" and current.Hauptkategorie == "concurrent category"
    )
    assert asset_country.load_cache(cache).entries


def test_retry_unknown_drops_cached_nulls(workspace, monkeypatch):
    """Legacy caches stored failed lookups as null; --retry-unknown clears them."""
    source, cache = workspace
    cache.write_text('{"1": "Deutschland", "9": null}', encoding="utf-8")
    seen = {}

    original_refresh = asset_country.refresh_countries

    def spy(
        frame: pd.DataFrame, **kwargs: Any
    ) -> tuple[pd.DataFrame, asset_country.CountryCache, dict[str, int]]:
        result, cache, stats = original_refresh(frame, **kwargs)
        seen.update(cache.legacy_unverified)
        assert stats["dropped"] == 1
        return result, cache, stats

    monkeypatch.setattr(backfill_country, "refresh_countries", spy)

    _run(
        monkeypatch,
        "--input",
        str(source),
        "--cache",
        str(cache),
        "--dry-run",
        "--retry-unknown",
    )

    assert "9" not in seen, "null entry survived --retry-unknown"
    assert seen["1"] == "Deutschland", "resolved entries must be kept"


def test_add_resolution_band_coerces_empty_strings_without_raising():
    # app.py's upsert fills columns missing on either side with "" (app.py:147-152),
    # so real data can hold a float/"" mix in an otherwise-numeric hours column.
    # A naive `<=` comparison against "" raises TypeError - add_resolution_band
    # must coerce before classifying, marking completed unknown duration explicitly.
    df = pd.DataFrame(
        {
            "time_to_resolution_biz_hours": [0.5, "", 20.0],
            "is_done": [True, True, True],
        }
    )

    out = add_resolution_band(df)

    assert list(out[BAND_COLUMN][:1]) == [BAND_UNDER_1H]
    assert out[BAND_COLUMN][1] == "Unbekannte Dauer"


def test_add_resolution_band_warns_and_skips_when_column_missing(capsys):
    df = pd.DataFrame({"is_done": [True]})

    out = add_resolution_band(df)

    assert BAND_COLUMN not in out.columns
    captured = capsys.readouterr()
    assert "time_to_resolution_biz_hours" in captured.out
    assert "Länder tab" in captured.out
