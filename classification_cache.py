"""Versioned classification identities and transactional single-artifact storage."""

import argparse
import hashlib
import json
import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, BinaryIO, Never

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from file_io import (
    AtomicWriteError,
    FileSnapshotChangedError,
    atomic_write,
    path_lock,
    read_file_snapshot,
)

CLASSIFICATION_SCHEMA_VERSION = 2
SCHEMA_METADATA_KEY = b"evex_classification_schema_version"
_VERSION_BYTES = str(CLASSIFICATION_SCHEMA_VERSION).encode("ascii")
RESULT_COLUMNS = (
    "key",
    "text_hash",
    "resolution_bucket",
    "sub_bucket",
    "intent_label",
    "ai_mode",
    "confidence",
    "reasoning",
    "model",
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "error",
)
_IDENTITY_COLUMNS = (
    "text_digest",
    "config_digest",
    "completed_at",
    "verification_state",
)
_COLUMNS = (*RESULT_COLUMNS, *_IDENTITY_COLUMNS)
_TOKENS = ("input_tokens", "output_tokens", "reasoning_tokens")


class ClassificationCacheError(ValueError):
    """A bounded cache format, identity or revision-conflict cause."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _json_value(value: object) -> Any:
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise ClassificationCacheError("invalid_canonical_value")
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise ClassificationCacheError("invalid_canonical_value")


def _canonical(value: object) -> bytes:
    return json.dumps(
        _json_value(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _freeze(value: object) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    return tuple(_freeze(item) for item in value) if isinstance(value, list) else value


@dataclass(frozen=True)
class ClassifierConfig:
    """Credential-free immutable request/policy inputs governing cache reuse."""

    endpoint: str
    deployment: str
    api_version: str
    prompt_digest: str
    taxonomy_digest: str
    schema_version: int
    renderer_version: str
    policy_version: str
    generation: Mapping[str, object]

    def __post_init__(self) -> None:
        if not isinstance(self.generation, Mapping):
            raise ClassificationCacheError("invalid_configuration")
        for name in (
            "endpoint",
            "deployment",
            "api_version",
            "prompt_digest",
            "taxonomy_digest",
            "renderer_version",
            "policy_version",
        ):
            if not isinstance(getattr(self, name), str):
                raise ClassificationCacheError("invalid_configuration")
        if type(self.schema_version) is not int or self.schema_version < 1:
            raise ClassificationCacheError("invalid_configuration")
        object.__setattr__(self, "endpoint", self.endpoint.rstrip("/"))
        object.__setattr__(self, "generation", _freeze(_json_value(self.generation)))


def generation_arguments(generation: Mapping[str, object]) -> dict[str, Any]:
    """Materialize frozen settings as a fresh JSON-compatible SDK request copy.

    Args:
        generation: Valid canonical generation values, including nested frozen containers.

    Returns:
        Ordinary nested dictionaries/lists, independently mutable by the SDK.
    """
    return _json_value(generation)


@dataclass(frozen=True)
class ClassificationIdentity:
    """A ticket's rendered-text and complete configuration SHA-256 identity."""

    key: str
    text_digest: str
    config_digest: str


def configuration_fingerprint(config: ClassifierConfig) -> str:
    """Hash canonical request/policy data, never environment credentials.

    Args:
        config: Immutable endpoint/deployment, schema, prompt and generation inputs.

    Returns:
        Full SHA-256 of canonical sorted compact UTF-8 JSON.
    """
    return hashlib.sha256(
        _canonical(
            {field.name: getattr(config, field.name) for field in fields(config)}
        )
    ).hexdigest()


def classification_identity(
    key: str, text: str, config: ClassifierConfig
) -> ClassificationIdentity:
    """Identify exactly one rendered input under one configuration.

    Args:
        key: Nonempty ticket key.
        text: Validated rendered input, including its effective truncation.
        config: Captured classifier configuration.

    Returns:
        Full text/configuration digests alongside the caller's key.
    """
    if not isinstance(key, str) or not key.strip() or not isinstance(text, str):
        raise ClassificationCacheError("invalid_identity")
    return ClassificationIdentity(
        key,
        hashlib.sha256(text.encode("utf-8")).hexdigest(),
        configuration_fingerprint(config),
    )


def _missing(value: object) -> bool:
    return bool(pd.api.types.is_scalar(value) and pd.isna(value))


def _normalize(frame: pd.DataFrame, *, stored: bool = False) -> pd.DataFrame:
    if not isinstance(frame, pd.DataFrame) or not frame.columns.is_unique:
        raise ClassificationCacheError("invalid_columns")
    if not all(isinstance(column, str) for column in frame):
        raise ClassificationCacheError("invalid_columns")
    result = frame.copy()
    if stored and any(column not in result for column in _COLUMNS):
        raise ClassificationCacheError("missing_columns")
    for column in _COLUMNS:
        if column not in result:
            result[column] = pd.Series(index=result.index, dtype="object")
    for index, row in result.iterrows():
        if not isinstance(row["key"], str) or not row["key"].strip():
            raise ClassificationCacheError("invalid_key")
        digests = (row["text_digest"], row["config_digest"])
        absent = tuple(_missing(value) for value in digests)
        supplied_state = row["verification_state"]
        if _missing(supplied_state):
            if stored:
                raise ClassificationCacheError("invalid_verification_state")
            state = "legacy_unverified" if all(absent) else "current"
        elif not isinstance(supplied_state, str) or supplied_state not in (
            "current",
            "legacy_unverified",
        ):
            raise ClassificationCacheError("invalid_verification_state")
        else:
            state = supplied_state
        verified = state == "current"
        if verified and any(absent):
            raise ClassificationCacheError("invalid_identity")
        if verified:
            if not all(
                isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value)
                for value in digests
            ):
                raise ClassificationCacheError("invalid_identity")
            try:
                completed = pd.Timestamp(row["completed_at"])
                if (
                    pd.isna(completed)
                    or completed.tzinfo is None
                    or completed.utcoffset() is None
                ):
                    raise ValueError("invalid_completion_time")
            except (TypeError, ValueError, OverflowError):
                raise ClassificationCacheError("invalid_completion_time") from None
        result.loc[index, "verification_state"] = state
    try:
        result["completed_at"] = pd.to_datetime(
            result["completed_at"], utc=True, errors="raise"
        )
        result["confidence"] = pd.to_numeric(
            result["confidence"], errors="raise"
        ).astype("float64")
        for column in _TOKENS:
            result[column] = pd.to_numeric(result[column], errors="raise").astype(
                "Int64"
            )
    except (TypeError, ValueError, OverflowError):
        raise ClassificationCacheError("invalid_record") from None
    for column in set(_COLUMNS) - {*_TOKENS, "confidence", "completed_at"}:
        if not all(
            _missing(value) or isinstance(value, str) for value in result[column]
        ):
            raise ClassificationCacheError("invalid_record")
    result.attrs = {"classification_schema_version": CLASSIFICATION_SCHEMA_VERSION}
    return result


def _record_hash(record: Mapping[str, Any]) -> str:
    normalized: dict[str, Any] = {}
    for key, value in record.items():
        if _missing(value):
            normalized[key] = None
        elif isinstance(value, (datetime, pd.Timestamp)):
            normalized[key] = value.astimezone(UTC).isoformat()
        else:
            normalized[key] = value.item() if hasattr(value, "item") else value
    return hashlib.sha256(_canonical(normalized)).hexdigest()


def _deduplicate(frame: pd.DataFrame) -> pd.DataFrame:
    selected: dict[tuple[str, ...], tuple[Any, dict[str, Any]]] = {}
    legacy_counts: Counter[str] = Counter()
    for record in frame.to_dict("records"):
        identity: tuple[str, ...]
        digest = _record_hash(record)
        if record["verification_state"] == "current":
            identity = (
                "current",
                record["key"],
                record["text_digest"],
                record["config_digest"],
            )
            order: Any = (record["completed_at"], digest)
        else:
            legacy_counts[digest] += 1
            identity, order = (
                (
                    "legacy_unverified",
                    digest,
                    str(legacy_counts[digest]),
                ),
                digest,
            )
        if identity not in selected or order > selected[identity][0]:
            selected[identity] = order, record
    return _normalize(
        pd.DataFrame(
            [selected[key][1] for key in sorted(selected)], columns=frame.columns
        )
    )


def _combine(current: pd.DataFrame, incoming: pd.DataFrame) -> pd.DataFrame:
    # Legacy history has no reusable identity. Preserve each existing occurrence;
    # a caller flushing its captured history adds only multiplicity above disk.
    counts = Counter(
        _record_hash(record)
        for record in current.to_dict("records")
        if record["verification_state"] == "legacy_unverified"
    )
    seen: Counter[str] = Counter()
    additional = []
    for record in incoming.to_dict("records"):
        if record["verification_state"] == "legacy_unverified":
            digest = _record_hash(record)
            seen[digest] += 1
            if seen[digest] <= counts[digest]:
                continue
        additional.append(record)
    frames = [
        frame
        for frame in (current, pd.DataFrame(additional, columns=incoming.columns))
        if not frame.empty
    ]
    return (
        _deduplicate(_normalize(pd.concat(frames, ignore_index=True)))
        if frames
        else current
    )


def _decode(payload: bytes) -> tuple[pd.DataFrame, int | None]:
    try:
        table = pq.read_table(pa.BufferReader(payload))
    except (pa.ArrowException, OSError, ValueError):
        raise ClassificationCacheError("invalid_parquet") from None
    version = (table.schema.metadata or {}).get(SCHEMA_METADATA_KEY)
    if version is not None and version != _VERSION_BYTES:
        raise ClassificationCacheError("unsupported_classification_version")
    try:
        frame = table.to_pandas()
    except (pa.ArrowException, TypeError, ValueError):
        raise ClassificationCacheError("invalid_parquet") from None
    if version is None:
        # Preserve historical fields without authorizing their reuse. Only the
        # artifact's version metadata can establish current fingerprint ownership.
        frame["verification_state"] = "legacy_unverified"
    return _deduplicate(_normalize(frame, stored=version is not None)), (
        2 if version else None
    )


def load_classifications(path: Path) -> pd.DataFrame:
    """Read versioned results without locking, writing, or constructing a client.

    Args:
        path: Trusted local parquet artifact.

    Returns:
        All independent identities, with legacy rows explicitly unverified.

    Raises:
        ClassificationCacheError: Unsupported/corrupt cache or incoherent snapshot.
    """
    try:
        payload, _ = read_file_snapshot(Path(path).resolve())
    except FileNotFoundError:
        return _normalize(pd.DataFrame(columns=_COLUMNS))
    except FileSnapshotChangedError:
        raise ClassificationCacheError("cache_changed") from None
    return _decode(payload)[0]


def _write(path: Path, frame: pd.DataFrame) -> None:
    def serialize(handle: BinaryIO) -> None:
        table = pa.Table.from_pandas(frame, preserve_index=False)
        metadata = dict(table.schema.metadata or {})
        metadata[SCHEMA_METADATA_KEY] = _VERSION_BYTES
        pq.write_table(table.replace_schema_metadata(metadata), handle)

    atomic_write(path, serialize)


def _backup(path: Path, payload: bytes) -> Path:
    target = path.with_name(
        f"{path.name}.bak-classification-{datetime.now(UTC):%Y%m%d-%H%M%S-%f}"
    )
    if target.exists():
        raise ClassificationCacheError("backup_conflict")

    def serialize(handle: BinaryIO) -> None:
        handle.write(payload)

    atomic_write(target, serialize)
    return target


def merge_classifications(path: Path, incoming: pd.DataFrame) -> pd.DataFrame:
    """Reload, merge independent identities and atomically publish under one lock.

    Args:
        path: Owning artifact path; aliases resolve once to their canonical target.
        incoming: Prepared results; no model work may occur inside this transaction.

    Returns:
        The current full cache, with deterministic same-identity winners.
    """
    prepared = _normalize(incoming)
    path = Path(path).resolve()
    with path_lock(path):
        payload: bytes | None
        version: int | None
        try:
            read_payload, _ = read_file_snapshot(path)
        except FileNotFoundError:
            payload = None
            current, version = (
                _normalize(pd.DataFrame(columns=_COLUMNS)),
                CLASSIFICATION_SCHEMA_VERSION,
            )
        else:
            payload = read_payload
            current, version = _decode(read_payload)
        merged = _combine(current, prepared)
        if payload is not None and version is None:
            _backup(path, payload)
        if payload is None or version is None or not current.equals(merged):
            _write(path, merged)
        return merged


def select_classifications(
    cache: pd.DataFrame,
    identities: Mapping[str, ClassificationIdentity],
) -> pd.DataFrame:
    """Select at most one current matching text/configuration result per key.

    Args:
        cache: Full history returned by the storage API.
        identities: Exactly the requested valid ticket identities.

    Returns:
        Requested matching results only, safe for a one-to-one ticket-key join.
    """
    frame = _deduplicate(_normalize(cache))
    records = []
    for key, identity in identities.items():
        if key != identity.key:
            raise ClassificationCacheError("invalid_selection")
        match = frame[
            (frame["verification_state"] == "current")
            & (frame["key"] == key)
            & (frame["text_digest"] == identity.text_digest)
            & (frame["config_digest"] == identity.config_digest)
        ]
        if not match.empty:
            records.append(match.iloc[0].to_dict())
    return _normalize(pd.DataFrame(records, columns=frame.columns))


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise ClassificationCacheError("invalid_arguments")


def main(argv: Sequence[str] | None = None) -> int:
    """Inspect/migrate metadata: 0 success, 2 invalid/conflict, 3 I/O failure."""
    parser = _Parser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    migrate = commands.add_parser("migrate")
    migrate.add_argument("--path", type=Path, required=True)
    migrate.add_argument("--apply", action="store_true")
    migrate.add_argument("--expected-revision")
    try:
        args = parser.parse_args(argv)
        path = args.path.resolve()
        payload, revision = read_file_snapshot(path)
        frame, version = _decode(payload)
        if not args.apply:
            print(
                json.dumps(
                    {
                        "source_version": version,
                        "target_version": CLASSIFICATION_SCHEMA_VERSION,
                        "rows": len(frame),
                        "revision": revision,
                    }
                )
            )
            return 0
        if args.expected_revision != ":".join(map(str, revision)):
            raise ClassificationCacheError("revision_conflict")
        with path_lock(path):
            original, current_revision = read_file_snapshot(path)
            if current_revision != revision:
                raise ClassificationCacheError("revision_conflict")
            frame, version = _decode(original)
            backup = None
            if version is None:
                backup = _backup(path, original)
                _write(path, frame)
        print(
            json.dumps(
                {
                    "result": "applied" if backup else "already_current",
                    "backup": str(backup) if backup else None,
                }
            )
        )
        return 0
    except (ClassificationCacheError, FileSnapshotChangedError):
        print("invalid_or_conflicting_classification_cache")
        return 2
    except (OSError, AtomicWriteError):
        print("classification_cache_io_failed")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
