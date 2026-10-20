"""Desk identity and import boundaries, independent of Jira credentials."""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from typing import Any, Literal
from zoneinfo import ZoneInfo

BERLIN = ZoneInfo("Europe/Berlin")
# Earliest day the date pickers offer. Without an explicit minimum, Streamlit only
# reaches ten years before a picker's default, hiding older imported tickets.
EARLIEST_DAY = date(1970, 1, 1)


@dataclass(frozen=True)
class ServiceDesk:
    key: str
    label: str
    company: str
    project_id: str | None = None
    service_desk_id: str | None = None
    earliest_created: datetime | None = None


DESKS = (
    ServiceDesk("SDIPR", "Ipro", "IPRO"),
    ServiceDesk("SDAX", "Amparex", "Amparex"),
    ServiceDesk(
        "SDEU",
        "Euronet",
        "Euronet",
        "12521",
        "219",
    ),
)
BY_KEY = {desk.key: desk for desk in DESKS}
COMPANY_LABELS = [desk.label for desk in DESKS]


def selected_projects(projects=None):
    """Known desks in the given order without duplicates; all desks when None."""
    projects = tuple(dict.fromkeys(BY_KEY if projects is None else projects))
    if not projects or any(project not in BY_KEY for project in projects):
        raise ValueError("Bitte mindestens einen gültigen Service Desk wählen.")
    return projects


def berlin_day_start(day):
    return datetime.combine(day, time.min, tzinfo=BERLIN)


def berlin_day_end(day):
    """Last instant of a Berlin calendar day, so a picked end day counts fully."""
    return datetime.combine(day, time.max, tzinfo=BERLIN)


def as_utc(value):
    """Treat legacy naive CLI timestamps as Berlin dates, then normalize."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo("Europe/Berlin"))
    return value.astimezone(UTC)


def effective_start(project, start):
    start = as_utc(start) if start is not None else None
    desk = BY_KEY.get(project)
    cutoff = as_utc(desk.earliest_created) if desk and desk.earliest_created else None
    return max(start, cutoff) if start and cutoff else start or cutoff


def filter_companies(frame, labels):
    companies = [desk.company for desk in DESKS if desk.label in labels]
    return frame.loc[frame["firma"].isin(companies)].copy()


@dataclass(frozen=True)
class InvalidIssue:
    """Safe identity and reason for a rejected raw Jira record."""

    project: str
    key: str | None
    code: Literal["invalid_created"] = "invalid_created"


def filter_issue_window(
    project: str,
    issues: Iterable[dict[str, Any]],
    start: datetime | None,
    end: datetime | None,
    *,
    invalid_row_cb: Callable[[InvalidIssue], None] | None = None,
) -> list[dict[str, Any]]:
    """Enforce exact instants after Jira's minute-granularity search/checkpoints."""
    start = effective_start(project, start)
    end = as_utc(end) if end else None
    result = []
    for issue in issues:
        key = issue.get("key")
        if not isinstance(key, str) or not key.startswith(project + "-"):
            continue
        fields = issue.get("fields")
        value = fields.get("created") if isinstance(fields, Mapping) else None
        try:
            if not isinstance(value, str):
                raise ValueError("invalid creation date")
            created = as_utc(datetime.fromisoformat(value))
        except (ValueError, OverflowError):
            if invalid_row_cb:
                invalid_row_cb(InvalidIssue(project, key))
            continue
        if (start is None or created >= start) and (end is None or created <= end):
            result.append(issue)
    return result
