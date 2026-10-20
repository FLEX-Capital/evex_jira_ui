"""Shared completion and reporting-calendar policy."""

from datetime import datetime, time
from typing import Literal
from zoneinfo import ZoneInfo

import pandas as pd

CompletionState = Literal["done", "open", "unknown"]
BERLIN = ZoneInfo("Europe/Berlin")
BUSINESS_START = time(8)
BUSINESS_END = time(18)
HOLIDAY_SUBDIV = "BW"
LEGACY_DONE = "Fertig"
WORKING_DAY_HOURS = (
    datetime.combine(datetime.min.date(), BUSINESS_END)
    - datetime.combine(datetime.min.date(), BUSINESS_START)
).total_seconds() / 3600
ELAPSED_EDGES = (0, 1, 2, 4, 8, 24, 48, 72, 168, 336, 504)
ELAPSED_ORDER = tuple(
    f"{lower}–{upper}" for lower, upper in zip(ELAPSED_EDGES, ELAPSED_EDGES[1:])
) + ("> 504", "Unbekannte Dauer")
CLONE_TYPE_NAMES = ("Cloners",)


def completion_state(
    category_key: object, request_status: object, *, done_value: str = LEGACY_DONE
) -> CompletionState:
    """Normalize completion using stable identity, with a legacy label fallback.

    Args:
        category_key: Jira's stable status-category key, when available.
        request_status: Legacy request status label.
        done_value: Compatibility override for the legacy completed label.

    Returns:
        Known done/open state, or unknown when identity/status is unsupported.
    """
    if isinstance(category_key, str):
        key = category_key.strip().casefold()
        if key:
            if key == "done":
                return "done"
            return "open" if key in {"new", "indeterminate"} else "unknown"
    elif not (pd.api.types.is_scalar(category_key) and pd.isna(category_key)):
        return "unknown"
    if not isinstance(request_status, str) or not request_status.strip():
        return "unknown"
    return "done" if request_status.strip() == done_value else "open"
