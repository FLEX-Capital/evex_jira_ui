"""Inspect trusted local caches; migration writes require a reviewed revision."""

import argparse
import json
import os
import pickle  # nosec B403
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, Never

import pandas as pd

from cache_schema import (
    CURRENT_TICKET_SCHEMA,
    SCHEMA_ATTR,
    CacheReadError,
    CacheSchemaError,
    normalize_frame,
)
from data_loading import (
    CacheChangedError,
    ImportBusyError,
    cache_lock,
    import_operation_lock,
    load_data,
    save_data,
)
from file_io import AtomicWriteError, Revision, atomic_write
from settings import SettingsError, load_settings


@dataclass(frozen=True)
class MigrationPlan:
    """One inspected ticket artifact and its intended canonical schema."""

    path: Path
    revision: Revision
    source_version: int | None
    target_version: int
    rows: int


def _snapshot(path: Path) -> tuple[bytes, Revision, pd.DataFrame]:
    with path.open("rb") as handle:
        before = os.fstat(handle.fileno())
        payload = handle.read()
        after = os.fstat(handle.fileno())
    revision = (before.st_ino, before.st_size, before.st_mtime_ns)
    if revision != (after.st_ino, after.st_size, after.st_mtime_ns):
        raise CacheChangedError("cache_changed_during_inspection")
    try:
        # Trusted locally generated pickle only; inspection is not a sandbox
        # for deserializing artifacts supplied by third parties.
        frame = pickle.loads(payload)  # nosec B301
    except Exception:
        raise CacheReadError("cache_read_failed") from None
    if not isinstance(frame, pd.DataFrame):
        raise CacheReadError("cache_not_dataframe")
    return payload, revision, frame


def plan_migration(path: Path) -> MigrationPlan:
    """Inspect one opened artifact without creating directories or lock files."""
    path = Path(path).resolve()
    _, revision, frame = _snapshot(path)
    normalized = normalize_frame(frame)
    return MigrationPlan(
        path,
        revision,
        frame.attrs.get(SCHEMA_ATTR),
        CURRENT_TICKET_SCHEMA,
        len(normalized),
    )


def apply_migration(plan: MigrationPlan) -> Path | None:
    """Back up exact source bytes then publish v2, or return None if current.

    Args:
        plan: Previously inspected source; every field is revalidated under locks.

    Returns:
        The exact original backup path, or None for an already-current artifact.

    Raises:
        CacheChangedError: The inspected source has changed.
        ImportBusyError: Another importer owns the dataset.
        AtomicWriteError: Backup or publication failed; the source stays intact.
    """
    config = load_settings()
    with import_operation_lock(plan.path), cache_lock(plan.path):
        payload, revision, frame = _snapshot(plan.path)
        if revision != plan.revision:
            raise CacheChangedError("cache_changed_since_plan")
        normalized = normalize_frame(frame)
        current = MigrationPlan(
            plan.path,
            revision,
            frame.attrs.get(SCHEMA_ATTR),
            CURRENT_TICKET_SCHEMA,
            len(normalized),
        )
        if current != plan:
            raise CacheChangedError("migration_plan_mismatch")
        # The public loader also validates tenant ownership. Do this before any
        # backup, including explicit nondefault-tenant legacy rejection.
        normalized = load_data(path=plan.path, settings=config)
        if plan.source_version == CURRENT_TICKET_SCHEMA:
            return None
        backup = plan.path.with_name(
            f"{plan.path.name}.bak-schema-{datetime.now(UTC):%Y%m%d-%H%M%S-%f}"
        )
        if backup.exists():
            raise CacheChangedError("backup_already_exists")

        def serialize(handle: BinaryIO) -> None:
            handle.write(payload)

        atomic_write(backup, serialize)
        save_data(normalized, path=plan.path, settings=config)
        return backup


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise ValueError("invalid_arguments")


def _parse_revision(value: str | None) -> Revision:
    if value is None:
        raise ValueError("expected_revision_required")
    parts = value.split(":")
    if len(parts) != 3 or not all(part.isdecimal() for part in parts):
        raise ValueError("invalid_expected_revision")
    return int(parts[0]), int(parts[1]), int(parts[2])


def main(argv: Sequence[str] | None = None) -> int:
    """Run read-only inspection or explicitly revision-guarded migration.

    Returns:
        0 for success/no-op, 2 for invalid/stale/busy input, 3 for I/O failure.
    """
    parser = _Parser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    migrate = commands.add_parser("migrate")
    migrate.add_argument("--path", type=Path, required=True)
    migrate.add_argument("--apply", action="store_true")
    migrate.add_argument("--expected-revision")
    try:
        args = parser.parse_args(argv)
        expected = _parse_revision(args.expected_revision) if args.apply else None
        plan = plan_migration(args.path)
        if args.apply:
            if expected != plan.revision:
                raise CacheChangedError("cache_changed_since_plan")
            backup = apply_migration(plan)
            print(
                json.dumps(
                    {
                        "result": "applied" if backup else "already_current",
                        "backup": str(backup) if backup else None,
                    }
                )
            )
        else:
            print(json.dumps(asdict(plan), default=str))
        return 0
    except (
        CacheChangedError,
        ImportBusyError,
        CacheSchemaError,
        SettingsError,
        ValueError,
    ):
        print("invalid_or_conflicting_input")
        return 2
    except (OSError, AtomicWriteError, CacheReadError):
        print("cache_io_failed")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
