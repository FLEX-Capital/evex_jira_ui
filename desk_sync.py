"""Shared Jira import, category repair, safe merge, and durable commit service."""

import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pandas as pd

from category_sync import (
    CATEGORY_FIELDS,
    CategoryLookup,
    CategoryRepairReport,
    CategoryResolver,
    apply_category_repairs,
    prepare_category_repairs,
    text_value,
    unknown,
    unresolved_cells,
)
from service_desks import BY_KEY, InvalidIssue
from settings import load_settings


def write_json_atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", dir=path.parent, delete=False
        ) as handle:
            temporary = handle.name
            json.dump(value, handle)
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def merge_import(current, incoming, baseline):
    """Keep rows changed during the pull and rows newer than its Jira snapshot."""
    from data_transformation import upsert_jira_data

    current = current if current is not None else pd.DataFrame()
    baseline = baseline if baseline is not None else pd.DataFrame()
    if incoming.empty:
        return current.copy(), 0
    if current.empty or "key" not in current:
        return upsert_jira_data(current, incoming), 0
    cached = current.drop_duplicates("key", keep="last").set_index("key")
    fresh = incoming.drop_duplicates("key", keep="last").set_index("key")
    overlap = cached.index.intersection(fresh.index)
    protected = pd.Index([])
    if "updated" in cached and "updated" in fresh:
        old_times = pd.to_datetime(
            cached.loc[overlap, "updated"], errors="coerce", utc=True
        )
        new_times = pd.to_datetime(
            fresh.loc[overlap, "updated"], errors="coerce", utc=True
        )
        protected = overlap[
            old_times.notna() & (new_times.isna() | (old_times > new_times))
        ]
    if not baseline.empty and "key" in baseline:
        before = baseline.drop_duplicates("key", keep="last").set_index("key")
        shared = overlap.intersection(before.index)
        columns = cached.columns.union(before.columns, sort=False)
        left = cached.reindex(index=shared, columns=columns).astype(object)
        right = before.reindex(index=shared, columns=columns).astype(object)
        unchanged = (left.eq(right) | (left.isna() & right.isna())).all(axis=1)
        protected = protected.union(shared[~unchanged])
        protected = protected.union(overlap.difference(before.index))
    else:
        # Rows added after an initially empty cache belong to a newer writer.
        protected = protected.union(overlap)
    accepted = incoming.loc[~incoming["key"].isin(protected)]
    return upsert_jira_data(current, accepted), len(protected)


@dataclass(frozen=True)
class SyncRequest:
    start: datetime
    end: datetime
    projects: tuple[str, ...]
    resume: bool = False
    repair_sources: bool = False
    publish_raw: bool = False

    def to_dict(self):
        return {
            **asdict(self),
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
        }


def make_sync_request(
    start, end, projects=None, *, resume=False, repair_sources=False, publish_raw=False
):
    from service_desks import BERLIN, as_utc, selected_projects

    now = datetime.now(UTC)
    start, end = as_utc(start), as_utc(end or now)
    if end.astimezone(BERLIN).date() == now.astimezone(BERLIN).date():
        end = min(end, now)
    if start > end:
        raise ValueError("Das Startdatum muss vor oder auf dem Enddatum liegen.")
    return SyncRequest(
        start, end, selected_projects(projects), resume, repair_sources, publish_raw
    )


@dataclass
class SyncReport:
    counts: dict = field(default_factory=dict)
    errors: dict = field(default_factory=dict)
    asset_failures: dict = field(default_factory=dict)
    invalid_rows: dict[str, int] = field(default_factory=dict)
    rows: int = 0
    protected_rows: int = 0
    categories: CategoryRepairReport = field(default_factory=CategoryRepairReport)
    source_updates: int = 0
    cache_revision: tuple | None = None
    raw_error: str | None = None
    source_error: str | None = None
    save_error: str | None = None
    committed: bool = False

    @property
    def complete(self):
        return not (
            self.errors
            or any(self.asset_failures.values())
            or any(self.invalid_rows.values())
            or self.categories.unresolved_cells
            or self.raw_error
            or self.source_error
            or self.save_error
        )

    def to_dict(self):
        return {**asdict(self), "complete": self.complete}


def status_path(data_path):
    return Path(data_path).parent / "import_status.json"


def _seed_category_context(issues, resolver, references):
    from jira_loader import CLOUD_ID, WORKSPACE_ID

    for issue in issues:
        fields = issue.get("fields") or {}
        references[issue["key"]] = {
            k: fields[k]
            for k in [f[0] for f in CATEGORY_FIELDS] + ["updated"]
            if k in fields
        }
        for field_name, _, _ in CATEGORY_FIELDS:
            refs = fields.get(field_name) or []
            if not refs:
                continue
            ref = refs[0]
            oid = str(ref.get("objectId", ""))
            label = (issue.get("asset_labels") or {}).get(oid)
            if ref.get("workspaceId") == WORKSPACE_ID and label:
                resolver.results.setdefault(
                    (CLOUD_ID, WORKSPACE_ID, oid), CategoryLookup(str(label), None, 0)
                )


def _keep_valid_category_labels(baseline, incoming, references):
    from jira_loader import WORKSPACE_ID

    result = incoming.copy()
    if baseline.empty or "key" not in baseline or "key" not in incoming:
        return result
    prior = baseline.drop_duplicates("key", keep="last").set_index("key")
    for index, row in result.iterrows():
        key = row["key"]
        if key not in prior.index:
            continue
        for raw_field, id_col, label_col in CATEGORY_FIELDS:
            refs = references.get(key, {}).get(raw_field) or []
            if (
                refs
                and refs[0].get("workspaceId") == WORKSPACE_ID
                and text_value(row.get(id_col))
                == text_value(prior.loc[key].get(id_col))
                and unknown(row.get(label_col))
                and not unknown(prior.loc[key].get(label_col))
            ):
                result.loc[index, label_col] = prior.loc[key, label_col]
    return result


def _repair_input(candidate, baseline):
    """Retained concurrent rows may still need the baseline's unknown repaired."""
    result = candidate.copy()
    if baseline.empty or "key" not in baseline or "key" not in result:
        return result
    prior = baseline.drop_duplicates("key", keep="last").set_index("key")
    for index, row in result.iterrows():
        if row["key"] in prior.index:
            old = prior.loc[row["key"]]
            for _, id_col, label_col in CATEGORY_FIELDS:
                if label_col in old and unknown(old[label_col]):
                    result.loc[index, label_col] = old[label_col]
                    result.loc[index, id_col] = old.get(id_col, "")
    return result


def _remaining_asset_failure(errors, resolver):
    from jira_loader import CLOUD_ID, WORKSPACE_ID

    for error in errors:
        repaired = False
        for field_name, _, _ in CATEGORY_FIELDS:
            prefix = f"{field_name}/"
            if error.startswith(prefix):
                oid = error[len(prefix) :].split(":", 1)[0]
                lookup = resolver.results.get((CLOUD_ID, WORKSPACE_ID, oid))
                repaired = lookup is not None and lookup.label is not None
        if not repaired:
            return True
    return False


def publish_raw_snapshot(checkpoint_dir, projects, target, *, run_id, request):
    """Publish one project at a time; the raw array is diagnostic, not the cache."""
    directory = Path(target).parent
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", dir=directory, delete=False
        ) as handle:
            temporary = handle.name
            handle.write("[")
            first = True
            count = 0
            for project in projects:
                with (
                    Path(checkpoint_dir) / f"backfill_{project}.json"
                ).open() as source:
                    issues = json.load(source)["issues"]
                for issue in issues:
                    if not first:
                        handle.write(",")
                    json.dump(issue, handle)
                    first = False
                    count += 1
                del issues
            handle.write("]")
        with open(temporary, "rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        os.replace(temporary, target)
        # Two replacements cannot be atomic together. A matching digest ties
        # metadata to this exact array; a metadata failure is reported separately
        # from the ticket-cache commit and cannot validate an older sidecar.
        write_json_atomic(
            Path(target).with_suffix(".meta.json"),
            {
                "version": 1,
                "run_id": run_id,
                "request": request.to_dict(),
                "published_at": datetime.now(UTC).isoformat(),
                "snapshot": Path(target).name,
                "sha256": digest,
                "issues": count,
            },
        )
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def run_sync(request, *, progress=None, data_path=None):
    """Download once, reconcile with current data, repair, and commit autonomously."""
    import data_loading
    import sync_checkpoints
    from data_transformation import load_project_issues
    from source_sync import refresh_missing_sources

    # Freeze paths before handing off a worker; never consult a mutable default later.
    data_path = Path(data_path or load_settings().paths.tickets).resolve()
    request = make_sync_request(
        request.start,
        request.end,
        request.projects,
        resume=request.resume,
        repair_sources=request.repair_sources,
        publish_raw=request.publish_raw,
    )
    report = SyncReport()
    with data_loading.import_operation_lock(data_path):
        status = {
            "version": 1,
            "run_id": uuid4().hex,
            "request": request.to_dict(),
            "state": "running",
            "progress": "Import wird gestartet",
            "report": None,
        }

        def emit(message, phase="fetch", project=None):
            status["progress"] = message
            write_json_atomic(status_path(data_path), status)
            if progress:
                progress({"phase": phase, "message": message, "project": project})

        emit("Import wird gestartet", "admitted")
        try:
            baseline, _ = data_loading.load_data_snapshot(path=data_path)
            baseline = baseline if baseline is not None else pd.DataFrame()
            frames, references, failures = [], {}, {}
            from asset_country import load_cache

            country_cache = load_cache(data_path.parent / "asset_country.json")
            resolver = CategoryResolver()

            def invalid_row(event: InvalidIssue) -> None:
                report.invalid_rows[event.project] = (
                    report.invalid_rows.get(event.project, 0) + 1
                )

            for project in request.projects:
                try:
                    emit(
                        f"{BY_KEY[project].label}: Tickets werden geladen",
                        project=project,
                    )
                    issues = sync_checkpoints.fetch_project(
                        project,
                        request.start,
                        request.end,
                        request.resume,
                        checkpoint_dir=data_path.parent,
                        category_resolver=resolver,
                        invalid_row_cb=invalid_row,
                        progress=lambda message, project=project: emit(
                            message, project=project
                        ),
                    )
                    _seed_category_context(issues, resolver, references)
                    if issues:
                        frames.append(
                            load_project_issues(
                                project, issues, country_cache=country_cache
                            )
                        )
                    failures[project] = [
                        i.get("asset_errors", [])
                        for i in issues
                        if i.get("asset_errors")
                    ]
                    report.counts[project] = len(issues)
                    del issues
                except Exception as exc:  # noqa: BLE001 - isolate a failed desk
                    report.errors[project] = f"{type(exc).__name__}: {exc}"
            if report.counts:
                incoming = (
                    pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
                )
                incoming = _keep_valid_category_labels(baseline, incoming, references)
                candidate, _ = merge_import(baseline, incoming, baseline)
                emit(
                    "Unbekannte Haupt- und Unterkategorien werden aufgelöst",
                    "categories",
                )
                patches, report.categories = prepare_category_repairs(
                    _repair_input(candidate, baseline),
                    references=references,
                    resolver=resolver,
                )
                source_patches = []
                if request.repair_sources and not candidate.empty:
                    try:
                        repaired, _ = refresh_missing_sources(candidate)
                        for index, row in candidate.iterrows():
                            if not text_value(row.get("source")) and text_value(
                                repaired.loc[index].get("source")
                            ):
                                source_patches.append(
                                    (
                                        row["key"],
                                        repaired.loc[index, "source"],
                                        row.get("updated"),
                                    )
                                )
                    except Exception as exc:  # noqa: BLE001 - source repair is optional
                        report.source_error = type(exc).__name__
                report.asset_failures = {
                    p: sum(_remaining_asset_failure(e, resolver) for e in errors)
                    for p, errors in failures.items()
                }
                emit("Ergebnisse werden gespeichert", "commit")
                with data_loading.cache_lock(data_path):
                    current, revision = data_loading.load_data_snapshot(path=data_path)
                    current = current if current is not None else pd.DataFrame()
                    merged, report.protected_rows = merge_import(
                        current, incoming, baseline
                    )
                    before_repairs = merged.copy()
                    merged, report.categories.skipped_cells = apply_category_repairs(
                        merged, patches
                    )
                    report.categories.repaired_cells = 0
                    report.categories.unassigned_cells = 0
                    for patch in patches:
                        before = before_repairs.loc[before_repairs["key"] == patch.key]
                        after = merged.loc[merged["key"] == patch.key]
                        if (
                            len(before) == len(after) == 1
                            and unknown(before.iloc[0].get(patch.label_column))
                            and text_value(after.iloc[0].get(patch.label_column))
                            == patch.label
                        ):
                            if patch.label == "Nicht zugeordnet":
                                report.categories.unassigned_cells += 1
                            else:
                                report.categories.repaired_cells += 1
                    for key, source, expected_updated in source_patches:
                        indexes = merged.index[merged["key"] == key]
                        if len(indexes) == 1:
                            index = indexes[0]
                            if not text_value(
                                merged.loc[index].get("source")
                            ) and text_value(
                                merged.loc[index].get("updated")
                            ) == text_value(
                                expected_updated
                            ):
                                merged.loc[index, "source"] = source
                                report.source_updates += 1
                    report.categories.unresolved_cells = unresolved_cells(merged)
                    if not current.equals(merged):
                        data_loading.save_data_if_unchanged(
                            merged, revision, backup_prefix="sync", path=data_path
                        )
                        report.committed = True
                    report.rows = len(merged)
                    report.cache_revision = data_loading.cache_revision(data_path)
                if request.publish_raw and not report.errors:
                    try:
                        publish_raw_snapshot(
                            data_path.parent,
                            request.projects,
                            data_path.parent / "jira_issues.json",
                            run_id=status["run_id"],
                            request=request,
                        )
                    except (
                        Exception
                    ) as exc:  # noqa: BLE001 - cache was already committed
                        report.raw_error = type(exc).__name__
        except Exception as exc:  # noqa: BLE001 - persist failure before releasing lock
            report.save_error = f"{type(exc).__name__}: {exc}"
        status["report"] = report.to_dict()
        status["state"] = (
            "completed"
            if report.complete
            else ("partial" if report.committed else "failed")
        )
        status["cache_revision"] = report.cache_revision
        write_json_atomic(status_path(data_path), status)
    return report
