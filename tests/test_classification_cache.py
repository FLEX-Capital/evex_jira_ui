"""Configuration identities, single-artifact versions and concurrent classifier writes."""

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

NOW = datetime(2026, 10, 20, 12, tzinfo=UTC)


def _config(**changes: Any) -> Any:
    from classification_cache import ClassifierConfig

    base = ClassifierConfig(
        "https://example.test",
        "deployment",
        "2024-10-21",
        "prompt",
        "taxonomy",
        2,
        "1",
        "1",
        {"max_completion_tokens": 4000, "max_chars": 6000},
    )
    return replace(base, **changes) if changes else base


def _record(
    key: str = "SDAX-1",
    text: str = "same",
    *,
    config: Any = None,
    when: datetime = NOW,
    label: str = "left",
) -> dict[str, Any]:
    from classification_cache import classification_identity

    identity = classification_identity(key, text, config or _config())
    return {
        "key": key,
        "text_digest": identity.text_digest,
        "config_digest": identity.config_digest,
        "text_hash": identity.text_digest[:16],
        "completed_at": when,
        "resolution_bucket": "question_answer",
        "sub_bucket": "how_to",
        "intent_label": label,
        "ai_mode": "human_only",
        "confidence": 0.9,
        "reasoning": "Synthetic",
        "model": "deployment",
        "input_tokens": 10,
        "output_tokens": 5,
        "reasoning_tokens": 0,
        "error": None,
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"endpoint": "https://other.test"},
        {"deployment": "other"},
        {"api_version": "other"},
        {"prompt_digest": "other"},
        {"taxonomy_digest": "other"},
        {"schema_version": 3},
        {"renderer_version": "2"},
        {"policy_version": "2"},
        {"generation": {"max_completion_tokens": 5000, "max_chars": 6000}},
        {"generation": {"max_completion_tokens": 4000, "max_chars": 5000}},
        {
            "generation": {
                "max_completion_tokens": 4000,
                "max_chars": 6000,
                "temperature": 1.0,
            }
        },
    ],
)
def test_config_identity_covers_every_policy_input(changes: dict[str, Any]) -> None:
    """Every request/policy input changes identity, while a trailing slash does not."""
    from classification_cache import classification_identity, configuration_fingerprint

    config = _config()
    fingerprint = configuration_fingerprint(config)
    assert len(fingerprint) == 64
    assert configuration_fingerprint(replace(config, **changes)) != fingerprint
    assert (
        configuration_fingerprint(replace(config, endpoint="https://example.test/"))
        == fingerprint
    )
    identity = classification_identity("SDAX-1", "same", config)
    assert identity == classification_identity("SDAX-1", "same", config)
    assert identity != classification_identity("SDAX-1", "changed", config)


def test_config_snapshot_is_independent_of_mutable_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutable caller settings and credential rotation cannot mutate an identity."""
    from classification_cache import configuration_fingerprint

    generation = {"max_completion_tokens": 4000, "max_chars": 6000}
    config = _config(generation=generation)
    before = configuration_fingerprint(config)
    generation["max_chars"] = 1
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "SYNTHETIC_PRIVATE_MARKER")
    assert configuration_fingerprint(config) == before
    assert "SYNTHETIC_PRIVATE_MARKER" not in repr(config)


def test_independent_process_writers_retain_all_identities(tmp_path: Path) -> None:
    """Two simultaneous processes retain distinct text/config/key results and a shared identity."""
    from classification_cache import load_classifications

    path = tmp_path / "cache.parquet"
    script = """import sys,time
from pathlib import Path
import pandas as pd
from datetime import timedelta
from test_classification_cache import _record, _config, NOW
from classification_cache import merge_classifications
path,number=Path(sys.argv[1]),int(sys.argv[2])
path.with_name(f'ready-{number}').touch()
deadline=time.monotonic()+10
while len(list(path.parent.glob('ready-*')))<2:
    if time.monotonic()>deadline: raise RuntimeError('barrier timeout')
    time.sleep(.01)
rows=[_record('SDAX-'+str(number+2), text=str(number)),
      _record(config=_config(policy_version=str(number+1)),text='variant'),
      _record(when=NOW+timedelta(seconds=number),label=str(number))]
merge_classifications(path,pd.DataFrame(rows))
"""
    env = dict(
        os.environ,
        PYTHONPATH=os.pathsep.join((str(Path.cwd()), str(Path.cwd() / "tests"))),
        PYTHONDONTWRITEBYTECODE="1",
    )
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(path), str(i)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for i in range(2)
    ]
    try:
        for process in processes:
            stdout, stderr = process.communicate(timeout=20)
            assert process.returncode == 0, stdout + stderr
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait()
    cache = load_classifications(path)
    assert len(cache) == 5
    shared = cache[
        (cache.key == "SDAX-1")
        & (cache.text_digest == hashlib.sha256(b"same").hexdigest())
    ]
    assert len(shared) == 1 and shared.iloc[0].intent_label == "1"
    assert not list(tmp_path.glob(".evex-atomic-*.tmp"))


def _record_digest(record: dict[str, Any]) -> str:
    normalized: dict[str, Any] = {}
    for key, value in record.items():
        if pd.isna(value):
            normalized[key] = None
        elif isinstance(value, (datetime, pd.Timestamp)):
            normalized[key] = value.astimezone(UTC).isoformat()
        else:
            normalized[key] = value.item() if hasattr(value, "item") else value
    return hashlib.sha256(
        json.dumps(
            normalized,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    ).hexdigest()


def test_identical_identity_tie_is_canonical_and_order_independent(
    tmp_path: Path,
) -> None:
    """Equal-time ties choose the larger canonical record hash in either write order."""
    from classification_cache import merge_classifications

    left, right = _record(label="left"), _record(label="right")
    samples = [
        merge_classifications(tmp_path / f"single-{i}.parquet", pd.DataFrame([record]))
        .iloc[0]
        .to_dict()
        for i, record in enumerate((left, right))
    ]
    expected = max(samples, key=_record_digest)["intent_label"]
    for i, ordered in enumerate(((left, right), (right, left))):
        path = tmp_path / f"order-{i}.parquet"
        for record in ordered:
            selected = merge_classifications(path, pd.DataFrame([record]))
        assert len(selected) == 1 and selected.iloc[0].intent_label == expected


@pytest.mark.parametrize("stage", ["serialize", "replace"])
def test_interrupted_classification_write_keeps_valid_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    """Failed serialization/replacement keeps exact readable bytes and removes owned temps."""
    import file_io
    from classification_cache import load_classifications, merge_classifications
    from file_io import AtomicWriteError

    path = tmp_path / "cache.parquet"
    merge_classifications(path, pd.DataFrame([_record()]))
    before = path.read_bytes()

    def fail(*args: Any, **kwargs: Any) -> None:
        raise OSError("SYNTHETIC_PRIVATE_MARKER")

    monkeypatch.setattr(
        pq if stage == "serialize" else file_io.os,
        "write_table" if stage == "serialize" else "replace",
        fail,
    )
    with pytest.raises(AtomicWriteError):
        merge_classifications(path, pd.DataFrame([_record(key="SDAX-2")]))
    assert path.read_bytes() == before and len(load_classifications(path)) == 1
    assert not list(tmp_path.glob(".evex-atomic-*.tmp"))


def test_legacy_migration_never_invents_current_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inspect/apply is offline, explicit, revision-guarded and backed up exactly."""
    import ticket_classifier
    from classification_cache import (
        classification_identity,
        load_classifications,
        main,
        select_classifications,
    )
    from file_io import read_file_snapshot

    path = tmp_path / "legacy.parquet"
    old = _record()
    for name in ("text_digest", "config_digest", "completed_at"):
        old.pop(name)
    pd.DataFrame([old]).to_parquet(path, index=False)
    original = path.read_bytes()
    monkeypatch.setattr(
        ticket_classifier, "build_client", lambda *a, **k: pytest.fail("model client")
    )
    assert main(["migrate", "--path", str(path)]) == 0
    cache = load_classifications(path)
    identity = classification_identity("SDAX-1", "same", _config())
    assert select_classifications(cache, {"SDAX-1": identity}).empty
    assert list(tmp_path.iterdir()) == [path] and path.read_bytes() == original
    assert main(["migrate", "--path", str(path), "--apply"]) == 2
    _, revision = read_file_snapshot(path)
    assert (
        main(
            [
                "migrate",
                "--path",
                str(path),
                "--apply",
                "--expected-revision",
                ":".join(map(str, revision)),
            ]
        )
        == 0
    )
    backups = list(tmp_path.glob("legacy.parquet.bak-*"))
    assert len(backups) == 1 and backups[0].read_bytes() == original
    metadata = pq.read_schema(path).metadata
    assert (
        metadata is not None and metadata[b"evex_classification_schema_version"] == b"2"
    )
    migrated = load_classifications(path)
    assert migrated.iloc[0].text_hash == old["text_hash"]
    assert migrated.iloc[0].verification_state == "legacy_unverified"
    assert select_classifications(migrated, {"SDAX-1": identity}).empty


@pytest.mark.parametrize("kind", ["future", "corrupt"])
def test_future_and_corrupt_classification_cache_fail_safely(
    tmp_path: Path, kind: str
) -> None:
    """Unsupported/corrupt files cannot masquerade as an empty reusable cache."""
    from classification_cache import ClassificationCacheError, load_classifications

    path = tmp_path / "cache.parquet"
    if kind == "corrupt":
        path.write_bytes(b"SYNTHETIC_PRIVATE_MARKER")
    else:
        table = pa.Table.from_pandas(pd.DataFrame([_record()]))
        pq.write_table(
            table.replace_schema_metadata(
                {b"evex_classification_schema_version": b"99"}
            ),
            path,
        )
    with pytest.raises(ClassificationCacheError) as caught:
        load_classifications(path)
    assert "SYNTHETIC_PRIVATE_MARKER" not in str(caught.value)


def test_selection_has_at_most_one_matching_result_per_key(tmp_path: Path) -> None:
    """Historical models/texts remain stored but never inflate a requested ticket join."""
    from classification_cache import (
        classification_identity,
        merge_classifications,
        select_classifications,
    )

    config = _config()
    rows = [
        _record(),
        _record(text="changed"),
        _record(config=_config(policy_version="2")),
        _record(key="SDAX-2"),
        _record(when=NOW + timedelta(seconds=1), label="newest"),
    ]
    cache = merge_classifications(tmp_path / "cache.parquet", pd.DataFrame(rows))
    selected = select_classifications(
        cache, {"SDAX-1": classification_identity("SDAX-1", "same", config)}
    )
    assert len(cache) == 4 and selected.key.tolist() == ["SDAX-1"]
    assert selected.iloc[0].intent_label == "newest"


def test_malformed_current_state_has_safe_cache_error(tmp_path: Path) -> None:
    """Asserted v2 metadata with a missing row state fails through the cache boundary."""
    from classification_cache import (
        ClassificationCacheError,
        load_classifications,
        merge_classifications,
    )

    path = tmp_path / "cache.parquet"
    frame = merge_classifications(path, pd.DataFrame([_record()]))
    frame["verification_state"] = pd.Series(pd.NA, index=frame.index, dtype="string")
    table = pa.Table.from_pandas(frame, preserve_index=False)
    metadata = dict(table.schema.metadata or {})
    metadata[b"evex_classification_schema_version"] = b"2"
    pq.write_table(table.replace_schema_metadata(metadata), path)
    with pytest.raises(ClassificationCacheError):
        load_classifications(path)


def test_unversioned_identity_fields_are_preserved_but_unverified(
    tmp_path: Path,
) -> None:
    """Missing artifact metadata does not authorize reuse or discard historical fields."""
    from classification_cache import (
        classification_identity,
        load_classifications,
        select_classifications,
    )

    path = tmp_path / "legacy.parquet"
    record = _record()
    pd.DataFrame([record]).to_parquet(path, index=False)
    frame = load_classifications(path)
    assert frame.iloc[0].text_digest == record["text_digest"]
    assert frame.iloc[0].completed_at == record["completed_at"]
    assert select_classifications(
        frame, {"SDAX-1": classification_identity("SDAX-1", "same", _config())}
    ).empty


def test_interrupted_legacy_migration_preserves_source_and_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed parquet publish keeps exact original bytes and the completed backup."""
    from classification_cache import main
    from file_io import read_file_snapshot

    path = tmp_path / "legacy.parquet"
    record = _record()
    for name in ("text_digest", "config_digest", "completed_at"):
        record.pop(name)
    pd.DataFrame([record]).to_parquet(path, index=False)
    original, revision = read_file_snapshot(path)

    def fail(*args: Any, **kwargs: Any) -> None:
        raise OSError("SYNTHETIC_PRIVATE_MARKER")

    monkeypatch.setattr(pq, "write_table", fail)
    assert (
        main(
            [
                "migrate",
                "--path",
                str(path),
                "--apply",
                "--expected-revision",
                ":".join(map(str, revision)),
            ]
        )
        == 3
    )
    assert path.read_bytes() == original
    backups = list(tmp_path.glob("legacy.parquet.bak-*"))
    assert len(backups) == 1 and backups[0].read_bytes() == original
    assert not list(tmp_path.glob(".evex-atomic-*.tmp"))


def test_metadata_migration_keeps_duplicate_legacy_rows(tmp_path: Path) -> None:
    """Metadata migration preserves historical multiplicity without multiplying later flushes."""
    from classification_cache import load_classifications, main, merge_classifications
    from file_io import read_file_snapshot

    path = tmp_path / "legacy.parquet"
    record = _record()
    for name in ("text_digest", "config_digest", "completed_at"):
        record.pop(name)
    pd.DataFrame([record, record, {**record, "model": "historical-other"}]).to_parquet(
        path, index=False
    )
    original, revision = read_file_snapshot(path)
    before = load_classifications(path)
    assert len(before) == 3
    assert (
        main(
            [
                "migrate",
                "--path",
                str(path),
                "--apply",
                "--expected-revision",
                ":".join(map(str, revision)),
            ]
        )
        == 0
    )
    migrated = load_classifications(path)
    assert len(migrated) == 3
    assert len(merge_classifications(path, migrated)) == 3
    assert next(tmp_path.glob("legacy.parquet.bak-*")).read_bytes() == original
