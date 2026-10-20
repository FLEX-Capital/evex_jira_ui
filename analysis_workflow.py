"""Pure cohort selection and safe provenance for the maintained analysis."""

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import pandas as pd

from service_desks import berlin_day_end, berlin_day_start

_INPUT_FIELDS = (
    "key",
    "created",
    "summary",
    "description",
    "comments",
    "Hauptkategorie",
    "Unterkategorie",
    "main_category_id",
    "sub_category_id",
    "issue_links",
    "clone_inward",
    "clone_outward",
    "clone_in_project",
    "status",
    "status_category_key",
    "completion_state",
    "resolution_band",
    "resolution",
    "time_to_resolution_biz_hours",
)


@dataclass(frozen=True)
class AnalysisSpec:
    """An exact project and inclusive Berlin window with an explicit classifier policy."""

    project: str
    start: date
    end: date
    classification_policy_version: str

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", self.project):
            raise ValueError("invalid_analysis_project")
        if type(self.start) is not date or type(self.end) is not date or self.start > self.end:
            raise ValueError("invalid_analysis_window")
        if not self.classification_policy_version.strip():
            raise ValueError("missing_classification_policy_version")


def select_cohort(frame: pd.DataFrame, spec: AnalysisSpec) -> pd.DataFrame:
    """Copy exact project/window rows, recording mutually exclusive exclusion counts."""
    keys = frame.get("key", pd.Series(index=frame.index, dtype="string")).astype("string")
    project = keys.str.fullmatch(re.escape(spec.project) + r"-\d+", na=False)
    values = (
        frame["created"]
        if "created" in frame
        else pd.Series(pd.NaT, index=frame.index, dtype="datetime64[ns, UTC]")
    )
    created = pd.to_datetime(values, format="mixed", errors="coerce", utc=True)
    if created is None:
        created = pd.Series(pd.NaT, index=frame.index, dtype="datetime64[ns, UTC]")
    valid = created.notna()
    window = created.between(berlin_day_start(spec.start), berlin_day_end(spec.end))
    result = frame.loc[project & valid & window].copy()
    result["created"] = created.loc[result.index]
    result = result.sort_values("key", kind="stable").reset_index(drop=True)
    if result["key"].duplicated().any():
        raise ValueError("duplicate_analysis_key")
    result.attrs["analysis_selection"] = {
        "selected_rows": len(result),
        "invalid_created_rows": int((project & ~valid).sum()),
        "excluded_window_rows": int((project & valid & ~window).sum()),
        "excluded_project_rows": int((~project).sum()),
    }
    return result


def _canonical(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _canonical(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat() if value.tzinfo else value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if hasattr(value, "item"):
        return _canonical(value.item())
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    raise ValueError("unsupported_analysis_input")


def cohort_digest(frame: pd.DataFrame) -> str:
    """Hash selected input/category/link/metric fields in stable key order, excluding attrs."""
    records = (
        frame.reindex(columns=_INPUT_FIELDS).sort_values("key", kind="stable").to_dict("records")
    )
    payload = json.dumps(
        _canonical(records), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def analysis_manifest(
    frame: pd.DataFrame,
    spec: AnalysisSpec,
    *,
    extracted_at: datetime,
    extraction_digest: str,
    classifier_fingerprint: str,
) -> dict[str, object]:
    """Return aggregate-only provenance; source bytes and selected inputs have separate hashes."""
    if extracted_at.tzinfo is None:
        raise ValueError("naive_extraction_timestamp")
    if not all(
        re.fullmatch(r"[0-9a-f]{64}", value)
        for value in (extraction_digest, classifier_fingerprint)
    ):
        raise ValueError("invalid_analysis_fingerprint")
    counts = frame.attrs.get("analysis_selection", {"selected_rows": len(frame)})
    return {
        "version": 1,
        "project": spec.project,
        "start": spec.start.isoformat(),
        "end": spec.end.isoformat(),
        **counts,
        "extracted_at": extracted_at.astimezone(UTC).isoformat(),
        "extraction_digest": extraction_digest,
        "cohort_digest": cohort_digest(frame),
        "classifier_fingerprint": classifier_fingerprint,
        "classification_policy_version": spec.classification_policy_version,
    }
