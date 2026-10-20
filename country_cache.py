"""Full tenant/object identity for country lookup and persistence."""

import argparse
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, BinaryIO, Literal, Never

from file_io import (
    AtomicWriteError,
    FileSnapshotChangedError,
    atomic_write,
    path_lock,
    read_file_snapshot,
    write_json_atomic,
)

COUNTRY_CACHE_VERSION = 2


@dataclass(frozen=True)
class AssetIdentity:
    """An Assets object is identified by cloud, workspace and object together."""

    cloud_id: str
    workspace_id: str
    object_id: str


class CountryCacheError(ValueError):
    """A safe country-cache schema or maintenance conflict code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class CountryEntry:
    """A verified full-identity country outcome with its UTC completion time."""

    identity: AssetIdentity
    outcome: Literal["found", "no_country"]
    country: str | None
    fetched_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.identity, AssetIdentity) or not all(
            isinstance(value, str) and bool(value.strip())
            for value in (
                self.identity.cloud_id,
                self.identity.workspace_id,
                self.identity.object_id,
            )
        ):
            raise CountryCacheError("invalid_identity")
        if (
            not isinstance(self.fetched_at, datetime)
            or self.fetched_at.tzinfo is None
            or self.fetched_at.utcoffset() is None
        ):
            raise CountryCacheError("invalid_fetched_at")
        if self.outcome == "found":
            if not isinstance(self.country, str) or not self.country.strip():
                raise CountryCacheError("invalid_country")
            object.__setattr__(self, "country", self.country.strip())
        elif self.outcome != "no_country" or self.country is not None:
            raise CountryCacheError("invalid_outcome")
        object.__setattr__(self, "fetched_at", self.fetched_at.astimezone(UTC))


@dataclass
class CountryCache:
    """Verified entries and retained unowned legacy values are separate state."""

    entries: dict[AssetIdentity, CountryEntry] = field(default_factory=dict)
    legacy_unverified: dict[str, str | None] = field(default_factory=dict)

    def copy(self) -> "CountryCache":
        """Return independent dictionaries; immutable entries may be shared."""
        return CountryCache(dict(self.entries), dict(self.legacy_unverified))

    def __len__(self) -> int:
        return len(self.entries) + len(self.legacy_unverified)


def is_country_fresh(entry: CountryEntry, *, now: datetime) -> bool:
    """Found values expire at seven days, verified empty outcomes at one day."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise CountryCacheError("invalid_clock")
    ttl = timedelta(days=7 if entry.outcome == "found" else 1)
    return now.astimezone(UTC) < entry.fetched_at + ttl


def _entry_record(entry: CountryEntry) -> dict[str, Any]:
    return {
        "cloud_id": entry.identity.cloud_id,
        "workspace_id": entry.identity.workspace_id,
        "object_id": entry.identity.object_id,
        "outcome": entry.outcome,
        "country": entry.country,
        "fetched_at": entry.fetched_at.isoformat(),
    }


def _entry_order(entry: CountryEntry) -> tuple[datetime, str]:
    record_hash = hashlib.sha256(
        json.dumps(_entry_record(entry), sort_keys=True).encode()
    ).hexdigest()
    return entry.fetched_at, record_hash


def choose_country_entry(
    current: CountryEntry | None, incoming: CountryEntry
) -> CountryEntry:
    """Choose newest UTC time, then canonical-record hash for an exact tie."""
    return (
        incoming
        if current is None or _entry_order(incoming) > _entry_order(current)
        else current
    )


def _legacy_values(value: object) -> dict[str, str | None]:
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and (country is None or isinstance(country, str))
        for key, country in value.items()
    ):
        raise CountryCacheError("invalid_legacy_metadata")
    return dict(value)


def _decode(payload: bytes) -> tuple[CountryCache, int | None]:
    try:
        raw = json.loads(payload)
    except (ValueError, UnicodeError):
        raise CountryCacheError("invalid_json") from None
    if not isinstance(raw, dict):
        raise CountryCacheError("invalid_payload")
    if "version" not in raw:
        return CountryCache({}, _legacy_values(raw)), None
    if type(raw["version"]) is not int or raw["version"] != COUNTRY_CACHE_VERSION:
        raise CountryCacheError("unsupported_country_version")
    values = raw.get("entries")
    if not isinstance(values, list):
        raise CountryCacheError("invalid_entries")
    cache = CountryCache({}, _legacy_values(raw.get("legacy_unverified", {})))
    for value in values:
        if not isinstance(value, dict):
            raise CountryCacheError("invalid_entry")
        try:
            identity = AssetIdentity(
                value["cloud_id"], value["workspace_id"], value["object_id"]
            )
            entry = CountryEntry(
                identity,
                value["outcome"],
                value["country"],
                datetime.fromisoformat(value["fetched_at"]),
            )
        except (KeyError, TypeError, ValueError, OverflowError):
            raise CountryCacheError("invalid_entry") from None
        if identity in cache.entries:
            raise CountryCacheError("duplicate_identity")
        cache.entries[identity] = entry
    return cache, COUNTRY_CACHE_VERSION


def load_country_cache(path: Path) -> CountryCache:
    """Read one artifact; legacy values remain unverified and are never rewritten."""
    try:
        payload, _ = read_file_snapshot(Path(path))
    except FileNotFoundError:
        return CountryCache()
    return _decode(payload)[0]


def _envelope(cache: CountryCache) -> dict[str, Any]:
    entries = sorted(
        cache.entries.values(),
        key=lambda entry: (
            entry.identity.cloud_id,
            entry.identity.workspace_id,
            entry.identity.object_id,
        ),
    )
    return {
        "version": COUNTRY_CACHE_VERSION,
        "entries": [_entry_record(entry) for entry in entries],
        "legacy_unverified": cache.legacy_unverified,
    }


def _backup_original(path: Path, payload: bytes) -> Path:
    backup = path.with_name(
        f"{path.name}.bak-country-{datetime.now(UTC):%Y%m%d-%H%M%S-%f}"
    )
    if backup.exists():
        raise CountryCacheError("backup_conflict")

    def serialize(handle: BinaryIO) -> None:
        handle.write(payload)

    atomic_write(backup, serialize)
    return backup


def merge_country_entries(
    path: Path,
    incoming: Sequence[CountryEntry],
    *,
    legacy: Mapping[str, str | None] | None = None,
    baseline: Mapping[str, str | None] | None = None,
) -> CountryCache:
    """Publish newest verified identities under the country lock, retaining peers.

    Explicit legacy metadata updates use baseline comparison and never create
    verified tenant entries. Converting an existing legacy artifact takes an
    exact backup before publication; ordinary reads never enter this function.
    """
    path = Path(path)
    with path_lock(path):
        version: int | None
        payload: bytes | None
        try:
            read_payload, _ = read_file_snapshot(path)
        except FileNotFoundError:
            payload = None
            current, version = CountryCache(), COUNTRY_CACHE_VERSION
        else:
            payload = read_payload
            current, version = _decode(read_payload)
        updated = current.copy()
        for entry in incoming:
            if not isinstance(entry, CountryEntry):
                raise CountryCacheError("invalid_entry")
            before = updated.entries.get(entry.identity)
            updated.entries[entry.identity] = choose_country_entry(before, entry)
        if legacy is not None:
            values = _legacy_values(dict(legacy))
            if baseline is None:
                updated.legacy_unverified.update(values)
            else:
                missing = object()
                for key in baseline.keys() | values.keys():
                    before_value = baseline.get(key, missing)
                    after_value = values.get(key, missing)
                    if (
                        before_value == after_value
                        or current.legacy_unverified.get(key, missing) != before_value
                    ):
                        continue
                    if after_value is missing:
                        updated.legacy_unverified.pop(key, None)
                    elif after_value is None or isinstance(after_value, str):
                        updated.legacy_unverified[key] = after_value
                    else:
                        raise CountryCacheError("invalid_legacy_metadata")
        if (
            version == COUNTRY_CACHE_VERSION
            and updated == current
            and payload is not None
        ):
            return current
        if version is None and payload is not None:
            _backup_original(path, payload)
        write_json_atomic(path, _envelope(updated))
        return updated


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise CountryCacheError("invalid_arguments")


def main(argv: Sequence[str] | None = None) -> int:
    """Inspect or explicitly migrate metadata; no tenant lookup/network is run.

    Returns:
        0 inspected/applied/already current, 2 invalid/conflicting, 3 I/O failure.
    """
    parser = _Parser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    migrate = commands.add_parser("migrate")
    migrate.add_argument("--path", type=Path, required=True)
    migrate.add_argument("--apply", action="store_true")
    migrate.add_argument("--expected-revision")
    try:
        args = parser.parse_args(argv)
        payload, revision = read_file_snapshot(args.path)
        cache, version = _decode(payload)
        if not args.apply:
            print(
                json.dumps(
                    {
                        "source_version": version,
                        "target_version": COUNTRY_CACHE_VERSION,
                        "entries": len(cache.entries),
                        "legacy_unverified": len(cache.legacy_unverified),
                        "revision": revision,
                    }
                )
            )
            return 0
        if args.expected_revision != ":".join(map(str, revision)):
            raise CountryCacheError("revision_conflict")
        with path_lock(args.path):
            original, current_revision = read_file_snapshot(args.path)
            if current_revision != revision:
                raise CountryCacheError("revision_conflict")
            cache, version = _decode(original)
            backup = None
            if version is None:
                backup = _backup_original(args.path, original)
                write_json_atomic(args.path, _envelope(cache))
        print(
            json.dumps(
                {
                    "result": "applied" if backup else "already_current",
                    "backup": str(backup) if backup else None,
                }
            )
        )
        return 0
    except (CountryCacheError, FileSnapshotChangedError):
        print("invalid_or_conflicting_input")
        return 2
    except (OSError, AtomicWriteError):
        print("country_cache_io_failed")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
