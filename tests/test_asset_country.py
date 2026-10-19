"""Escalation, bucket and cache tests for asset_country."""

import json

import pandas as pd

from asset_country import (
    NO_ASSETS_LINKED,
    NO_COUNTRY_ON_ASSET,
    NO_SOURCE,
    NOT_RESOLVED,
    attach_country,
    classify_country,
    load_cache,
    normalise_object_id,
    resolve_missing,
    save_cache,
)

CACHE = {"1": "Deutschland", "2": "Österreich", "3": None}


def test_ansprechpartner_wins_over_filiale_and_zentrale():
    assert classify_country("ID_2", "ID_1", "ID_1", CACHE) == (
        "Österreich",
        "ansprechpartner",
    )


def test_falls_through_to_filiale_when_ansprechpartner_has_no_land():
    assert classify_country("ID_3", "ID_1", None, CACHE) == ("Deutschland", "filiale")


def test_falls_through_to_zentrale():
    assert classify_country("", "", "ID_2", CACHE) == ("Österreich", "zentrale")


def test_no_assets_at_all():
    assert classify_country("", "", "", CACHE) == (NO_ASSETS_LINKED, NO_SOURCE)


def test_assets_present_but_none_carry_land():
    assert classify_country("ID_3", "ID_3", "ID_3", CACHE) == (
        NO_COUNTRY_ON_ASSET,
        NO_SOURCE,
    )


def test_id_absent_from_cache_is_unresolved_not_land_less():
    # "3" is cached as None: fetched, carries no Land.
    assert classify_country("ID_3", "", "", CACHE) == (NO_COUNTRY_ON_ASSET, NO_SOURCE)
    # "999" was never looked up, so we cannot claim anything about its Land.
    assert classify_country("ID_999", "", "", CACHE) == (NOT_RESOLVED, NO_SOURCE)


def test_a_resolved_country_outranks_an_unresolved_earlier_link():
    # Ansprechpartner is unknown to the cache, but Filiale has a real answer;
    # a real country always beats "we do not know yet".
    assert classify_country("ID_999", "ID_1", "", CACHE) == ("Deutschland", "filiale")


def test_unresolved_outranks_land_less_when_both_present():
    # "3" carries no Land and "999" was never fetched - resolving "999" could
    # still produce a country, so the ticket is not yet "Kein Land am Asset".
    assert classify_country("ID_3", "ID_999", "", CACHE) == (NOT_RESOLVED, NO_SOURCE)


def test_normalise_strips_prefix_and_detects_absence():
    assert normalise_object_id("ID_42") == "42"
    assert normalise_object_id("42") == "42"
    assert normalise_object_id("") is None
    assert normalise_object_id("nan") is None
    assert normalise_object_id("None") is None
    assert normalise_object_id(None) is None
    assert normalise_object_id(float("nan")) is None


def test_attach_country_adds_columns_without_mutating():
    df = pd.DataFrame(
        {
            "key": ["A-1", "A-2", "A-3"],
            "ansprechpartner": ["", "ID_3", ""],
            "filiale": ["", "", ""],
            "zentrale": ["ID_1", "", ""],
        }
    )
    before = df.copy()
    out = attach_country(df, CACHE)
    pd.testing.assert_frame_equal(df, before)
    assert list(out["Land"]) == ["Deutschland", NO_COUNTRY_ON_ASSET, NO_ASSETS_LINKED]
    assert list(out["land_quelle"]) == ["zentrale", NO_SOURCE, NO_SOURCE]


def test_attach_country_tolerates_missing_column():
    df = pd.DataFrame({"key": ["A-1"], "zentrale": ["ID_1"], "filiale": [""]})
    out = attach_country(df, CACHE)
    assert list(out["Land"]) == ["Deutschland"]


def test_resolve_missing_only_fetches_unknown_ids():
    calls = []

    def fake(object_id):
        calls.append(object_id)
        return "Schweiz"

    cache = {"1": "Deutschland"}
    cache, resolved, failed = resolve_missing(
        ["ID_1", "ID_5", "ID_5", ""], cache, resolver=fake
    )
    assert calls == ["5"]
    assert cache["5"] == "Schweiz"
    assert (resolved, failed) == (1, 0)


def test_resolver_failure_is_counted_not_cached():
    def boom(object_id):
        raise RuntimeError("403")

    cache, resolved, failed = resolve_missing(["ID_7"], {}, resolver=boom)
    assert "7" not in cache  # a failed lookup must not look like "no Land"
    assert (resolved, failed) == (0, 1)


def test_default_resolver_reports_failure_by_raising(monkeypatch):
    """The not-cached guarantee above only holds if the default resolver raises.

    resolve_asset_country() swallows fetch errors and returns None, which
    resolve_missing() would cache as a real "no Land" answer and never retry.
    """
    import jira_country_export

    def boom(object_id, cloud_id=None, workspace_id=None):
        raise RuntimeError("403")

    monkeypatch.setattr(jira_country_export, "_fetch_asset_with_retry", boom)

    # No resolver= argument: this exercises the lazy default import.
    cache, resolved, failed = resolve_missing(["ID_7"], {})
    assert "7" not in cache
    assert (resolved, failed) == (0, 1)


def test_default_resolver_still_caches_a_genuine_absence(monkeypatch):
    """An asset that fetches fine but has no Land is cached as None."""
    import jira_country_export

    monkeypatch.setattr(
        jira_country_export,
        "_fetch_asset_with_retry",
        lambda object_id, cloud_id=None, workspace_id=None: {"attributes": []},
    )

    cache, resolved, failed = resolve_missing(["ID_8"], {})
    assert cache == {"8": None}
    assert (resolved, failed) == (1, 0)


def test_cache_roundtrip(tmp_path):
    path = tmp_path / "cache.json"
    save_cache({"1": "Deutschland", "2": None}, str(path))
    assert load_cache(str(path)) == {"1": "Deutschland", "2": None}
    assert json.loads(path.read_text(encoding="utf-8"))["1"] == "Deutschland"


def test_load_cache_missing_file(tmp_path):
    assert load_cache(str(tmp_path / "absent.json")) == {}


def test_stale_country_writers_preserve_each_others_additions(tmp_path):
    path = str(tmp_path / "cache.json")
    first, second = load_cache(path), load_cache(path)
    first["1"] = "Deutschland"
    second["2"] = "Österreich"
    save_cache(first, path)
    save_cache(second, path)
    assert load_cache(path) == {"1": "Deutschland", "2": "Österreich"}


def test_country_commit_preserves_concurrent_change_and_removes_only_unchanged_nulls(
    tmp_path,
):
    path = str(tmp_path / "cache.json")
    save_cache({"1": None, "2": None, "3": None}, path)
    baseline = load_cache(path)
    # A second job resolves an id that the first job only saw as null.
    save_cache({"1": "Deutschland"}, path)
    merged = save_cache({"3": None, "4": "Österreich"}, path, baseline=baseline)
    assert merged == {"1": "Deutschland", "3": None, "4": "Österreich"}
    assert load_cache(path) == merged


def test_country_commits_from_separate_processes_preserve_all_additions(tmp_path):
    import subprocess
    import sys

    path = str(tmp_path / "cache.json")
    script = (
        "import sys; from asset_country import save_cache; "
        "save_cache({sys.argv[2]: sys.argv[2]}, sys.argv[1], baseline={})"
    )
    processes = [
        subprocess.Popen([sys.executable, "-c", script, path, str(i)]) for i in range(4)
    ]
    try:
        for process in processes:
            assert process.wait(timeout=30) == 0
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait()
    assert load_cache(path) == {str(i): str(i) for i in range(4)}
