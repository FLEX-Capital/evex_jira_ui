"""Resolve category labels and prepare identity-checked, narrow cache repairs."""

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from typing import Any

import pandas as pd
import requests

import service_desks as desks
from http_policy import CATEGORY, RetryDeferred, with_retries
from settings import Settings, load_settings

CATEGORY_FIELDS = desks.category_fields()
UNASSIGNED = "Nicht zugeordnet"


def text_value(value):
    return "" if value is None or pd.isna(value) else str(value).strip()


def unknown(value):
    return text_value(value).casefold() in {"", "unknown", "unbekannt"}


def timestamp(value):
    value = pd.to_datetime(value, errors="coerce", utc=True)
    return None if pd.isna(value) else value


@dataclass(frozen=True)
class CategoryLookup:
    label: str | None
    error: str | None
    attempts: int


class CategoryResolver:
    """One lookup budget per full identity, including failures, for one import."""

    def __init__(
        self,
        fetch: Callable[[str, str, str], Mapping[str, Any]] | None = None,
        sleep: Callable[[float], None] | None = None,
        *,
        settings: Settings | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings or load_settings()
        self.fetch = fetch
        self.sleep = sleep or time.sleep
        self.now = now or (lambda: datetime.now(UTC))
        self.results: dict[tuple[str, str, str], CategoryLookup] = {}

    def resolve(self, cloud_id, workspace_id, object_id):
        identity = (cloud_id, workspace_id, str(object_id))
        if identity in self.results:
            return self.results[identity]
        fetch = self.fetch
        if fetch is None:
            from jira_loader import fetch_asset_object_uncached

            fetch = partial(fetch_asset_object_uncached, settings=self.settings)
        attempts = 0

        def lookup() -> str:
            nonlocal attempts
            attempts += 1
            asset = fetch(*identity)
            if str(asset.get("id")) != identity[2]:
                raise ValueError("Assets returned a different object identity")
            label = text_value(asset.get("label") or asset.get("name"))
            if not label:
                raise ValueError("Assets object has no label")
            return label

        try:
            label = with_retries(
                lookup, policy=CATEGORY, now=self.now, sleep=self.sleep
            )
            result = CategoryLookup(label, None, attempts)
        except RetryDeferred:
            result = CategoryLookup(None, "deferred", attempts)
        except (requests.RequestException, PermissionError, ValueError) as exc:
            response = getattr(exc, "response", None)
            status = response.status_code if response is not None else None
            result = CategoryLookup(
                None, f"HTTP {status}" if status else type(exc).__name__, attempts
            )
        self.results[identity] = result
        return result


def fetch_category_references(keys, *, settings=None):
    from source_sync import fetch_issue_fields

    return fetch_issue_fields(
        keys, [f[0] for f in desks.category_fields()] + ["updated"], settings=settings
    )


@dataclass(frozen=True)
class CategoryPatch:
    key: str
    label_column: str
    id_column: str
    observed_id: str
    observed_label: str
    reference_id: str
    workspace_id: str
    label: str
    source_updated: str | None


@dataclass
class CategoryRepairReport:
    repaired_cells: int = 0
    unassigned_cells: int = 0
    unresolved_cells: int = 0
    failed_objects: dict = field(default_factory=dict)
    skipped_cells: int = 0


def prepare_category_repairs(
    frame, *, references=None, resolver=None, reference_loader=None, settings=None
):
    config = settings or getattr(resolver, "settings", None) or load_settings()
    tenant = config.tenant
    resolver = resolver or CategoryResolver(settings=config)
    references = dict(references or {})
    candidates = []
    for _, row in frame.iterrows():
        for raw_field, id_column, label_column in desks.category_fields():
            if (id_column in frame or label_column in frame) and unknown(
                row.get(label_column)
            ):
                candidates.append((row, raw_field, id_column, label_column))
    missing_keys = sorted(
        {text_value(r.get("key")) for r, *_ in candidates} - references.keys()
    )
    reference_error = "Jira category reference was not returned"
    if missing_keys:
        try:
            references.update(
                (
                    reference_loader
                    or partial(fetch_category_references, settings=config)
                )(missing_keys)
            )
        except (
            requests.RequestException,
            PermissionError,
            ValueError,
            RuntimeError,
        ) as exc:
            reference_error = type(exc).__name__
    report = CategoryRepairReport()
    patches = []
    for row, raw_field, id_column, label_column in candidates:
        key = text_value(row.get("key"))
        fields = references.get(key, {})
        refs = fields.get(raw_field)
        error = None
        oid, workspace, label = "", "", None
        if raw_field not in fields:
            error = reference_error
        elif refs is None or refs == []:
            label = UNASSIGNED
        elif not isinstance(refs, list) or not isinstance(refs[0], dict):
            error = "Invalid category reference"
        else:
            oid = text_value(refs[0].get("objectId"))
            workspace = refs[0].get("workspaceId")
            if not oid or workspace != tenant.workspace_id:
                error = "Unresolved workspace reference"
            else:
                lookup = resolver.resolve(tenant.cloud_id, workspace, oid)
                label, error = lookup.label, lookup.error
        if error:
            report.unresolved_cells += 1
            identity = (
                f"{tenant.cloud_id}/{workspace}/{oid}" if oid else f"{key}/{raw_field}"
            )
            report.failed_objects[identity] = error
            continue
        patches.append(
            CategoryPatch(
                key,
                label_column,
                id_column,
                text_value(row.get(id_column)),
                text_value(row.get(label_column)),
                oid,
                workspace or "",
                label,
                fields.get("updated"),
            )
        )
        if label == UNASSIGNED:
            report.unassigned_cells += 1
        else:
            report.repaired_cells += 1
    return patches, report


def apply_category_repairs(current, patches):
    result = current.copy()
    skipped = 0
    if "key" not in result:
        return result, len(patches)
    for patch in patches:
        matches = result.index[result["key"] == patch.key]
        if len(matches) != 1:
            skipped += 1
            continue
        index = matches[0]
        row = result.loc[index]
        cached_time, source_time = (
            timestamp(row.get("updated")),
            timestamp(patch.source_updated),
        )
        if (
            # The final merge may retain the observed baseline or accept the
            # downloaded row containing the verified replacement reference.
            # Equivalent unknown placeholders do not represent a new category.
            text_value(row.get(patch.id_column))
            not in {patch.observed_id, patch.reference_id}
            or not unknown(row.get(patch.label_column))
            or (
                cached_time is not None
                and (source_time is None or cached_time > source_time)
            )
        ):
            skipped += 1
            continue
        result.loc[index, patch.id_column] = patch.reference_id
        result.loc[index, patch.label_column] = patch.label
    return result, skipped


def unresolved_cells(frame):
    return sum(
        int(frame[label].map(unknown).sum())
        for _, _, label in desks.category_fields()
        if label in frame
    )
