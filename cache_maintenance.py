"""Inspect trusted local caches; migration writes require a reviewed revision."""

import argparse
import hashlib
import json
import os
import pickle  # nosec B403
import re
import stat
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import BinaryIO, Literal, Never

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
from file_io import AtomicWriteError, Revision, atomic_write, path_lock
from service_desks import BY_KEY
from settings import DataPaths, SettingsError, load_settings


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


Family = Literal["ticket", "country", "classifier"]
Inventory = dict[str, tuple[int, Revision]]


@dataclass(frozen=True)
class CleanupCandidate:
    """A recognized owned file and its inspected revision."""

    path: Path
    revision: Revision
    family: Family
    reason: str


@dataclass(frozen=True)
class CleanupPlan:
    """Reviewable candidates, protected paths and their inventory digest."""

    candidates: tuple[CleanupCandidate, ...]
    protected: tuple[Path, ...]
    digest: str


class CleanupError(RuntimeError):
    """Safe partial progress when cleanup cannot finish its approved plan."""

    def __init__(
        self, code: str, deleted: tuple[Path, ...], failed: tuple[Path, ...]
    ) -> None:
        self.code = code
        self.deleted = deleted
        self.failed = failed
        super().__init__(f"{code}: " + ", ".join(map(str, failed)))


def _targets(paths: DataPaths) -> dict[str, Family]:
    values: dict[Path, Family] = {
        paths.tickets: "ticket",
        paths.raw_snapshot: "ticket",
        paths.raw_metadata: "ticket",
        paths.import_status: "ticket",
        paths.countries: "country",
        paths.classifications: "classifier",
    }
    for project in BY_KEY:
        checkpoint = paths.checkpoint(project)
        values[checkpoint] = "ticket"
        values[checkpoint.with_suffix(".partial.json")] = "ticket"
    if any(path.parent != paths.root for path in values):
        raise ValueError("target_outside_data_root")
    return {path.name: family for path, family in values.items()}


def _inventory(root: Path) -> tuple[Inventory, tuple[Path, ...]]:
    if root.is_symlink():
        raise ValueError("unsafe_data_root")
    if not root.exists():
        return {}, ()
    entries: Inventory = {}
    locks = []
    for path in sorted(root.iterdir()):
        info = path.lstat()
        if path.name.endswith(".lock"):
            locks.append(path)
            continue
        entries[path.name] = (
            info.st_mode,
            (info.st_ino, info.st_size, info.st_mtime_ns),
        )
    return entries, tuple(locks)


def _cleanup_scan(paths: DataPaths, *, now: datetime) -> tuple[CleanupPlan, Inventory]:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("aware_now_required")
    now = now.astimezone(UTC)
    targets = _targets(paths)
    entries, locks = _inventory(paths.root)
    backup_pattern = re.compile(
        rf"^{re.escape(paths.tickets.name)}\.bak-[A-Za-z][A-Za-z0-9_-]*-(\d{{8}}-\d{{6}}-\d{{6}})$"
    )
    backups = []
    for name, (mode, _) in entries.items():
        matched = backup_pattern.fullmatch(name)
        if stat.S_ISREG(mode) and matched:
            try:
                timestamp = datetime.strptime(
                    matched.group(1), "%Y%m%d-%H%M%S-%f"
                ).replace(tzinfo=UTC)
            except ValueError:
                continue
            backups.append((name, timestamp))
    newest = {
        name
        for name, _ in sorted(
            backups, key=lambda item: (-item[1].timestamp(), item[0])
        )[:10]
    }
    candidates = []
    for name, timestamp in backups:
        if name not in newest and now - timestamp > timedelta(days=14):
            candidates.append(
                CleanupCandidate(
                    paths.root / name,
                    entries[name][1],
                    "ticket",
                    "backup_outside_retention",
                )
            )
    for name, (mode, revision) in entries.items():
        if not stat.S_ISREG(mode):
            continue
        for target, family in targets.items():
            # mkstemp currently emits eight alphanumeric/underscore characters;
            # accept longer owned identifiers, never arbitrary .tmp suffixes.
            if re.fullmatch(
                rf"\.evex-atomic-{re.escape(target)}-[A-Za-z0-9_-]{{8,}}\.tmp", name
            ):
                if now - datetime.fromtimestamp(
                    revision[2] / 1_000_000_000, UTC
                ) > timedelta(hours=24):
                    candidates.append(
                        CleanupCandidate(
                            paths.root / name, revision, family, "abandoned_temporary"
                        )
                    )
                break
    ordered = tuple(sorted(candidates, key=lambda candidate: candidate.path.name))
    chosen = {candidate.path.name for candidate in ordered}
    protected = tuple(
        sorted(
            [paths.root / name for name in entries if name not in chosen] + list(locks)
        )
    )
    signature = {
        "root": str(paths.root),
        "targets": targets,
        "entries": entries,
        "candidates": [asdict(candidate) for candidate in ordered],
    }
    digest = hashlib.sha256(
        json.dumps(signature, sort_keys=True, default=str).encode()
    ).hexdigest()
    return CleanupPlan(ordered, protected, digest), entries


def plan_cleanup(paths: DataPaths, *, now: datetime) -> CleanupPlan:
    """Inspect one data root without following symlinks or creating anything.

    Keeps the newest ten recognized ticket backups and all <=14 days old.
    Only owned atomic temporaries strictly older than 24 hours are eligible.
    """
    return _cleanup_scan(paths, now=now)[0]


@contextmanager
def _owner_lock(family: Family, paths: DataPaths) -> Iterator[None]:
    if family == "ticket":
        with (
            import_operation_lock(paths.tickets),
            cache_lock(paths.tickets, blocking=False),
        ):
            yield
    else:
        path = paths.countries if family == "country" else paths.classifications
        with path_lock(path, blocking=False):
            yield


def apply_cleanup(
    plan: CleanupPlan, paths: DataPaths, *, now: datetime
) -> tuple[Path, ...]:
    """Delete only reviewed candidates after all-owner preflight and revalidation.

    Args:
        plan: Inspected candidates and digest; stale-at-entry plans delete nothing.
        paths: The same configured root and filenames used during inspection.
        now: Aware clock used for retention boundaries.

    Returns:
        Exact removed paths. No cache-family locks overlap.

    Raises:
        CacheChangedError: The reviewed inventory changed before any deletion.
        BlockingIOError: A family writer is active; no deletion has started.
        ImportBusyError: An import owns the dataset; no deletion has started.
        CleanupError: A later change or unlink failure interrupted deletion;
            deleted and failed fields contain exact safe paths.
    """
    current, expected = _cleanup_scan(paths, now=now)
    if current.digest != plan.digest or current.candidates != plan.candidates:
        raise CacheChangedError("cleanup_plan_changed")
    if not plan.candidates:
        return ()
    families: tuple[Family, ...] = ("ticket", "country", "classifier")
    for family in families:
        with _owner_lock(family, paths):
            if _inventory(paths.root)[0] != expected:
                raise CacheChangedError("cleanup_inventory_changed")
    deleted: list[Path] = []
    for family in families:
        selected = [
            candidate for candidate in plan.candidates if candidate.family == family
        ]
        if not selected:
            continue
        try:
            with _owner_lock(family, paths):
                if _inventory(paths.root)[0] != expected:
                    raise CacheChangedError("cleanup_inventory_changed")
                for candidate in selected:
                    try:
                        candidate.path.unlink()
                    except OSError:
                        raise CleanupError(
                            "unlink_failed", tuple(deleted), (candidate.path,)
                        ) from None
                    deleted.append(candidate.path)
                    expected.pop(candidate.path.name)
        except (CacheChangedError, ImportBusyError, BlockingIOError):
            if deleted:
                raise CleanupError(
                    "cleanup_interrupted",
                    tuple(deleted),
                    tuple(candidate.path for candidate in selected),
                ) from None
            raise
    return tuple(deleted)


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


def _run_cleanup(args: argparse.Namespace) -> int:
    paths = (
        load_settings(dict(os.environ, DATA_DIR=args.data_dir))
        if args.data_dir is not None
        else load_settings()
    ).paths
    now = datetime.now(UTC)
    plan = plan_cleanup(paths, now=now)
    if args.apply:
        if args.plan_digest != plan.digest:
            raise CacheChangedError("cleanup_plan_changed")
        deleted = apply_cleanup(plan, paths, now=now)
        print(json.dumps({"deleted": list(map(str, deleted))}))
    else:
        print(json.dumps(asdict(plan), default=str))
    return 0


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
    cleanup = commands.add_parser("cleanup")
    cleanup.add_argument("--data-dir")
    cleanup.add_argument("--apply", action="store_true")
    cleanup.add_argument("--plan-digest")
    try:
        args = parser.parse_args(argv)
        if args.command == "cleanup":
            return _run_cleanup(args)
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
    except CleanupError as exc:
        print(
            json.dumps(
                {
                    "code": exc.code,
                    "deleted": list(map(str, exc.deleted)),
                    "failed": list(map(str, exc.failed)),
                }
            )
        )
        return 2 if exc.code == "cleanup_interrupted" else 3
    except (
        BlockingIOError,
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
