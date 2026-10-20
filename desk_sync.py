"""Shared Jira import, category repair, safe merge, and durable commit service."""

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any, BinaryIO
from uuid import uuid4

import pandas as pd

import service_desks as desks
from category_sync import (
    CategoryLookup,
    CategoryRepairReport,
    CategoryResolver,
    apply_category_repairs,
    prepare_category_repairs,
    text_value,
    unknown,
    unresolved_cells,
)
from file_io import atomic_write
from file_io import write_json_atomic as write_json_atomic
from issue_links import fetch_link_graph
from operation_errors import OperationDiagnostic, safe_diagnostic
from service_desks import BY_KEY, InvalidIssue
from settings import DEFAULT_TENANT, TENANT_ATTR, load_settings


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
    link_failures: dict[str, int] = field(default_factory=dict)
    diagnostics: list[OperationDiagnostic] = field(default_factory=list)
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
            or any(self.link_failures.values())
            or self.diagnostics
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
    tenant = resolver.settings.tenant

    for issue in issues:
        fields = issue.get("fields") or {}
        references[issue["key"]] = {
            k: fields[k]
            for k in [f[0] for f in desks.category_fields()] + ["updated"]
            if k in fields
        }
        for field_name, _, _ in desks.category_fields():
            refs = fields.get(field_name) or []
            if not refs:
                continue
            ref = refs[0]
            oid = str(ref.get("objectId", ""))
            label = (issue.get("asset_labels") or {}).get(oid)
            identity = (issue.get("assets_cloud_id"), issue.get("assets_workspace_id"))
            if identity != (tenant.cloud_id, tenant.workspace_id):
                if identity != (None, None) or tenant != DEFAULT_TENANT:
                    label = None
            if ref.get("workspaceId") == tenant.workspace_id and label:
                resolver.results.setdefault(
                    (tenant.cloud_id, tenant.workspace_id, oid),
                    CategoryLookup(str(label), None, 0),
                )


def _keep_valid_category_labels(baseline, incoming, references, *, settings=None):
    tenant = (settings or load_settings()).tenant

    result = incoming.copy()
    if baseline.attrs.get(TENANT_ATTR) != tenant.as_dict():
        return result
    if baseline.empty or "key" not in baseline or "key" not in incoming:
        return result
    prior = baseline.drop_duplicates("key", keep="last").set_index("key")
    for index, row in result.iterrows():
        key = row["key"]
        if key not in prior.index:
            continue
        for raw_field, id_col, label_col in desks.category_fields():
            refs = references.get(key, {}).get(raw_field) or []
            if (
                refs
                and refs[0].get("workspaceId") == tenant.workspace_id
                and text_value(row.get(id_col))
                == text_value(prior.loc[key].get(id_col))
                and unknown(row.get(label_col))
                and not unknown(prior.loc[key].get(label_col))
            ):
                result.loc[index, label_col] = prior.loc[key, label_col]
    return result


def _keep_known_links(baseline: pd.DataFrame, incoming: pd.DataFrame) -> pd.DataFrame:
    """Keep old second-hop evidence only while its direct links still match."""
    if baseline.empty or incoming.empty or "link_enrichment_complete" not in incoming:
        return incoming
    previous = baseline.drop_duplicates("key", keep="last").set_index("key")
    result = incoming.copy()
    columns = ("clones_of_clones", "clone_types_of_clones", "has_axt_clone_clone")
    for index, row in result.iterrows():
        if bool(row["link_enrichment_complete"]) or row["key"] not in previous.index:
            continue
        old = previous.loc[row["key"]]
        if row.get("issue_links") != old.get("issue_links"):
            continue
        for column in columns:
            if column in old and pd.isna(row.get(column)) and not pd.isna(old[column]):
                result.loc[index, column] = old[column]
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
            for _, id_col, label_col in desks.category_fields():
                if label_col in old and unknown(old[label_col]):
                    result.loc[index, label_col] = old[label_col]
                    result.loc[index, id_col] = old.get(id_col, "")
    return result


def _remaining_asset_failure(errors, resolver):
    tenant = resolver.settings.tenant

    for error in errors:
        repaired = False
        for field_name, _, _ in desks.category_fields():
            prefix = f"{field_name}/"
            if error.startswith(prefix):
                oid = error[len(prefix) :].split(":", 1)[0]
                lookup = resolver.results.get(
                    (tenant.cloud_id, tenant.workspace_id, oid)
                )
                repaired = lookup is not None and lookup.label is not None
        if not repaired:
            return True
    return False


def publish_raw_snapshot(checkpoint_dir, projects, target, *, run_id, request):
    """Publish one project at a time; the raw array is diagnostic, not the cache."""
    digest = hashlib.sha256()
    count = 0

    def serialize(handle: BinaryIO) -> None:
        nonlocal count

        def emit(value: bytes) -> None:
            handle.write(value)
            digest.update(value)

        emit(b"[")
        first = True
        for project in projects:
            with (Path(checkpoint_dir) / f"backfill_{project}.json").open() as source:
                issues = json.load(source)["issues"]
            for issue in issues:
                if not first:
                    emit(b",")
                emit(json.dumps(issue).encode("utf-8"))
                first = False
                count += 1
            del issues
        emit(b"]")

    atomic_write(Path(target), serialize)
    # A matching digest ties metadata to this exact array; two replacements
    # cannot be atomic together, so a sidecar failure never validates old data.
    write_json_atomic(
        Path(target).with_suffix(".meta.json"),
        {
            "version": 1,
            "run_id": run_id,
            "request": request.to_dict(),
            "published_at": datetime.now(UTC).isoformat(),
            "snapshot": Path(target).name,
            "sha256": digest.hexdigest(),
            "issues": count,
        },
    )


def run_sync(request, *, progress=None, data_path=None):
    """Download once, reconcile with current data, repair, and commit autonomously."""
    import data_loading
    import sync_checkpoints
    from data_transformation import load_project_issues
    from source_sync import (
        fetch_issue_fields,
        fetch_issue_sources,
        refresh_missing_sources,
    )

    # Freeze paths before handing off a worker; never consult a mutable default later.
    config = load_settings()
    data_path = Path(data_path or config.paths.tickets).resolve()
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

        def diagnose(
            exc: Exception,
            stage: str,
            project: str | None = None,
            key: str | None = None,
        ) -> str:
            cause = safe_diagnostic(
                exc,
                operation_id=status["run_id"],
                stage=stage,
                project=project,
                key=key,
            )
            report.diagnostics.append(cause)
            return cause.code

        emit("Import wird gestartet", "admitted")
        stage = "fetch"
        try:
            baseline, _ = data_loading.load_data_snapshot(
                path=data_path, settings=config
            )
            baseline = baseline if baseline is not None else pd.DataFrame()
            frames, references, failures = [], {}, {}
            from asset_country import load_cache

            country_cache = load_cache(
                data_path.parent / "asset_country.json", settings=config
            )
            resolver = CategoryResolver(settings=config)

            def invalid_row(event: InvalidIssue) -> None:
                report.invalid_rows[event.project] = (
                    report.invalid_rows.get(event.project, 0) + 1
                )
                report.diagnostics.append(
                    OperationDiagnostic(
                        status["run_id"],
                        "transform",
                        "invalid_created",
                        project=event.project,
                        key=event.key,
                    )
                )

            link_fields: dict[str, Any] = {}
            link_errors: dict[str, Exception] = {}

            def fetch_links(keys: Sequence[str]) -> dict[str, Any]:
                missing = [
                    key
                    for key in keys
                    if key not in link_fields and key not in link_errors
                ]
                if missing:
                    try:
                        received = fetch_issue_fields(
                            missing, ["issuelinks"], settings=config
                        )
                        for key in missing:
                            link_fields[key] = received.get(key)
                    except (
                        Exception
                    ) as exc:  # noqa: BLE001 - reuse a failed batch within this import
                        for key in missing:
                            link_errors[key] = exc
                # fetch_link_graph handles each optional batch failure without dropping tickets.
                # Return validated records individually so a previous failure does not hide peers.
                return {key: link_fields[key] for key in keys if key in link_fields}

            for project in request.projects:
                stage = "fetch"
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
                        settings=config,
                        invalid_row_cb=invalid_row,
                        progress=lambda message, project=project: emit(
                            message, project=project
                        ),
                    )
                    for value in issues:
                        link_fields[value["key"]] = value.get("fields") or {}
                        link_errors.pop(value["key"], None)
                    graph = fetch_link_graph(
                        issues, fetch_many=fetch_links, operation_id=status["run_id"]
                    )
                    causes = [
                        (
                            safe_diagnostic(
                                link_errors[cause.key],
                                operation_id=status["run_id"],
                                stage="links",
                                project=project,
                                key=cause.key,
                            )
                            if cause.key in link_errors
                            else cause
                        )
                        for cause in graph.diagnostics
                    ]
                    report.diagnostics.extend(causes)
                    report.link_failures[project] = len(causes)
                    stage = "transform"
                    _seed_category_context(issues, resolver, references)
                    if issues:
                        frames.append(
                            load_project_issues(
                                project,
                                issues,
                                country_cache=country_cache,
                                settings=config,
                                link_graph=graph,
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
                    report.errors[project] = diagnose(exc, stage, project)
            if report.counts:
                incoming = (
                    pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
                )
                incoming = _keep_known_links(baseline, incoming)
                incoming = _keep_valid_category_labels(
                    baseline, incoming, references, settings=config
                )
                candidate, _ = merge_import(baseline, incoming, baseline)
                emit(
                    "Unbekannte Haupt- und Unterkategorien werden aufgelöst",
                    "categories",
                )
                patches, report.categories = prepare_category_repairs(
                    _repair_input(candidate, baseline),
                    references=references,
                    resolver=resolver,
                    settings=config,
                )
                source_patches = []
                if request.repair_sources and not candidate.empty:
                    try:
                        repaired, _ = refresh_missing_sources(
                            candidate,
                            fetch_sources=partial(fetch_issue_sources, settings=config),
                        )
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
                        report.source_error = diagnose(exc, "source")
                report.asset_failures = {
                    p: sum(_remaining_asset_failure(e, resolver) for e in errors)
                    for p, errors in failures.items()
                }
                emit("Ergebnisse werden gespeichert", "commit")
                stage = "save"
                with data_loading.cache_lock(data_path):
                    current, revision = data_loading.load_data_snapshot(
                        path=data_path, settings=config
                    )
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
                        merged.attrs[TENANT_ATTR] = config.tenant.as_dict()
                        data_loading.save_data_if_unchanged(
                            merged,
                            revision,
                            backup_prefix="sync",
                            path=data_path,
                            settings=config,
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
                        report.raw_error = diagnose(exc, "raw")
        except Exception as exc:  # noqa: BLE001 - persist failure before releasing lock
            report.save_error = diagnose(exc, stage)
        status["report"] = report.to_dict()
        status["state"] = (
            "completed"
            if report.complete
            else ("partial" if report.committed else "failed")
        )
        status["cache_revision"] = report.cache_revision
        write_json_atomic(status_path(data_path), status)
    return report
