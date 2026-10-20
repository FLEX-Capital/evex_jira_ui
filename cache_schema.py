"""Pure normalization and validation for trusted local ticket DataFrames."""

from functools import lru_cache

import pandas as pd

from asset_country import attach_country
from reporting_policy import BERLIN

CURRENT_TICKET_SCHEMA = 2
SCHEMA_ATTR = "evex_ticket_schema_version"
_DATES = ("created", "updated", "currentstatus_date", "resolved_at")
_BOOLS = ("is_done", "has_exax_clone", "has_axt_clone_clone")
_LISTS = (
    "labels",
    "category",
    "issue_links",
    "clone_inward_keys",
    "clone_outward_keys",
    "asset_references",
)
_COUNTS = ("clone_count", "n_clones")


class CacheSchemaError(ValueError):
    """A safe schema code and known column names, never record contents."""

    def __init__(self, code: str, columns: tuple[str, ...] = ()) -> None:
        self.code = code
        self.columns = columns
        super().__init__(f"{code}: {', '.join(columns)}" if columns else code)


class CacheReadError(RuntimeError):
    """A safe failure to deserialize a trusted local ticket artifact."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _missing(value: object) -> bool:
    return bool(pd.api.types.is_scalar(value) and pd.isna(value))


def _valid_list(column: str, value: object) -> bool:
    if _missing(value):
        return True
    if not isinstance(value, list):
        return False
    if column == "asset_references":
        return all(
            isinstance(reference, dict)
            and reference.get("source") in ("ansprechpartner", "filiale", "zentrale")
            and reference.get("provenance") in ("modern", "legacy_unverified", "rejected")
            and (
                reference["provenance"] != "modern"
                or all(
                    isinstance(reference.get(key), str) and reference[key]
                    for key in ("cloud_id", "workspace_id", "object_id")
                )
            )
            for reference in value
        )
    if column == "issue_links":
        return all(
            isinstance(link, dict)
            and isinstance(link.get("type_name"), str)
            and bool(link["type_name"].strip())
            and link.get("direction") in ("inward", "outward")
            and isinstance(link.get("target_key"), str)
            and bool(link["target_key"].strip())
            for link in value
        )
    if column in ("clone_inward_keys", "clone_outward_keys"):
        return all(isinstance(key, str) and bool(key.strip()) for key in value)
    return True


@lru_cache(maxsize=1)
def _empty_schema() -> pd.DataFrame:
    from data_transformation import _ISSUE_COLUMNS, enrich_jira_time_metrics

    result = enrich_jira_time_metrics(pd.DataFrame(columns=_ISSUE_COLUMNS))
    for column in (
        "firma",
        "Hauptkategorie",
        "Unterkategorie",
        "Land",
        "land_quelle",
        "clone_in_project",
    ):
        result[column] = pd.Series(dtype="string")
    for column in _BOOLS:
        result[column] = pd.Series(dtype="boolean")
    for column in _COUNTS:
        result[column] = pd.Series(dtype="Int64")
    for column in result:
        if column in _LISTS:
            result[column] = pd.Series(dtype="object")
        elif column not in (*_DATES, *_BOOLS, *_COUNTS) and not (
            pd.api.types.is_numeric_dtype(result[column])
            or isinstance(result[column].dtype, pd.CategoricalDtype)
        ):
            result[column] = pd.Series(dtype="string")
    return result


def _validate(frame: pd.DataFrame) -> None:
    template = _empty_schema()
    missing = tuple(column for column in template if column not in frame)
    if missing:
        raise CacheSchemaError("missing_columns", missing)
    invalid = []
    for column in template:
        values = frame[column]
        if column in _DATES:
            valid = isinstance(values.dtype, pd.DatetimeTZDtype)
        elif column in _BOOLS:
            valid = pd.api.types.is_bool_dtype(values.dtype)
        elif pd.api.types.is_numeric_dtype(template[column].dtype):
            valid = pd.api.types.is_numeric_dtype(values.dtype) and not pd.api.types.is_bool_dtype(
                values.dtype
            )
        elif column in _LISTS:
            valid = all(_valid_list(column, value) for value in values)
        else:
            valid = all(_missing(value) or isinstance(value, str) for value in values)
        if not valid:
            invalid.append(column)
    if invalid:
        raise CacheSchemaError("invalid_types", tuple(invalid))
    if frame.key.duplicated().any():
        raise CacheSchemaError("duplicate_key", ("key",))
    if not frame.completion_state.isin(("done", "open", "unknown")).all():
        raise CacheSchemaError("invalid_completion_state", ("completion_state",))
    if not frame.duration_quality.isin(("valid", "missing", "reversed")).all():
        raise CacheSchemaError("invalid_duration_quality", ("duration_quality",))
    expected_done = frame.completion_state.map({"done": True, "open": False}).astype("boolean")
    if not frame.is_done.astype("boolean").equals(expected_done):
        raise CacheSchemaError("inconsistent_completion", ("completion_state", "is_done"))


def normalize_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Return a validated v2 copy, normalizing unversioned/v1 history in memory.

    Args:
        frame: Trusted local ticket data; unknown columns and attrs are retained.

    Returns:
        A separate canonical frame. This function never reads or writes storage.

    Raises:
        CacheSchemaError: Version, keys or an asserted v2 schema are invalid.
    """
    if not isinstance(frame, pd.DataFrame):
        raise CacheSchemaError("not_dataframe")
    version = frame.attrs.get(SCHEMA_ATTR)
    if SCHEMA_ATTR in frame.attrs and (type(version) is not int or version not in (1, 2)):
        raise CacheSchemaError("unsupported_schema_version")
    if not frame.columns.is_unique or not all(isinstance(column, str) for column in frame):
        raise CacheSchemaError("invalid_columns")
    if not frame.empty and (
        "key" not in frame
        or not all(isinstance(key, str) and bool(key.strip()) for key in frame.key)
    ):
        raise CacheSchemaError("invalid_key", ("key",))
    result = frame.copy(deep=True)
    if version == CURRENT_TICKET_SCHEMA:
        if "asset_references" not in result:
            result["asset_references"] = pd.Series(index=result.index, dtype="object")
        _validate(result)
        for column in _DATES:
            result[column] = result[column].dt.tz_convert(BERLIN.key)
        return result

    from data_transformation import enrich_jira_time_metrics

    template = _empty_schema()
    if result.empty:
        for column in template:
            result[column] = template[column].copy()
    else:
        result = result.reset_index(drop=True)
        times = pd.to_datetime(
            result.get("updated", pd.Series(index=result.index, dtype="object")),
            errors="coerce",
            utc=True,
            format="mixed",
        )
        # Stable input ordering breaks same-instant ties; invalid recency loses
        # to a known instant, without losing rows with otherwise unknown dates.
        order = times.reset_index(drop=True).sort_values(kind="stable", na_position="first").index
        result = result.iloc[order].drop_duplicates("key", keep="last")
        result = result.sort_index(kind="stable").reset_index(drop=True)
        result = enrich_jira_time_metrics(result)
        for column in template:
            if column not in result:
                result[column] = pd.Series(index=result.index, dtype=template[column].dtype)
        countries = attach_country(result, {})
        for column in ("Land", "land_quelle"):
            result[column] = result[column].fillna(countries[column])
    for column in _BOOLS:
        try:
            result[column] = result[column].astype("boolean")
        except (TypeError, ValueError, OverflowError):
            raise CacheSchemaError("invalid_legacy_type", (column,)) from None
    for column in _COUNTS:
        try:
            result[column] = pd.to_numeric(result[column], errors="coerce").astype("Int64")
        except (TypeError, ValueError, OverflowError):
            raise CacheSchemaError("invalid_legacy_type", (column,)) from None
    result.attrs = dict(frame.attrs)
    result.attrs[SCHEMA_ATTR] = CURRENT_TICKET_SCHEMA
    _validate(result)
    return result
