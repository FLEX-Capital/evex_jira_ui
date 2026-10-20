"""Desk identity and import boundaries, independent of Jira credentials."""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, fields
from datetime import UTC, date, datetime, time
from typing import Any, Literal, overload

import pandas as pd

from reporting_policy import BERLIN

# Earliest day the date pickers offer. Without an explicit minimum, Streamlit only
# reaches ten years before a picker's default, hiding older imported tickets.
EARLIEST_DAY = date(1970, 1, 1)


@dataclass(frozen=True)
class ServiceDesk:
    """A supported desk and its immutable reporting/import configuration."""

    key: str
    label: str
    company: str
    project_id: str | None = None
    service_desk_id: str | None = None
    earliest_created: datetime | None = None
    clone_project: str | None = None
    clone_of_clone_project: str | None = None
    comment_separator: str = "\n--------------------------------\n"


DESKS = (
    ServiceDesk("SDIPR", "Ipro", "IPRO", clone_project="EXIPR", comment_separator="\n\n"),
    ServiceDesk("SDAX", "Amparex", "Amparex", clone_project="EXAX", clone_of_clone_project="AX"),
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


@dataclass(frozen=True)
class JiraFields:
    """Supported custom fields shared by fetch, transformation and repair."""

    request: str = "customfield_10010"
    categories: str = "customfield_10065"
    central: str = "customfield_10673"
    branch: str = "customfield_10674"
    source: str = "customfield_10675"
    sub_category: str = "customfield_10679"
    main_category: str = "customfield_10680"
    contact: str = "customfield_10689"

    def retained(self) -> tuple[str, ...]:
        """Return the unique standard/custom field union consumed downstream."""
        standard = (
            "summary",
            "description",
            "status",
            "created",
            "updated",
            "labels",
            "priority",
            "issuetype",
            "issuelinks",
            "comment",
        )
        return tuple(dict.fromkeys((*standard, *(getattr(self, f.name) for f in fields(self)))))


FIELDS = JiraFields()


def category_fields() -> tuple[tuple[str, str, str], ...]:
    """Derive category repair columns from the active immutable field map."""
    return (
        (FIELDS.main_category, "main_category_id", "Hauptkategorie"),
        (FIELDS.sub_category, "sub_category_id", "Unterkategorie"),
    )


def selected_projects(projects: Iterable[str] | None = None) -> tuple[str, ...]:
    """Known desks in the given order without duplicates; all desks when None."""
    projects = tuple(dict.fromkeys(BY_KEY if projects is None else projects))
    if not projects or any(project not in BY_KEY for project in projects):
        raise ValueError("Bitte mindestens einen gültigen Service Desk wählen.")
    return projects


def berlin_day_start(day: date) -> datetime:
    """Return the first instant of a Berlin calendar day."""
    return datetime.combine(day, time.min, tzinfo=BERLIN)


def berlin_day_end(day: date) -> datetime:
    """Last instant of a Berlin calendar day, so a picked end day counts fully."""
    return datetime.combine(day, time.max, tzinfo=BERLIN)


def as_utc(value: datetime) -> datetime:
    """Treat legacy naive CLI timestamps as Berlin dates, then normalize."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=BERLIN)
    return value.astimezone(UTC)


@overload
def effective_start(project: str, start: datetime) -> datetime: ...


@overload
def effective_start(project: str, start: None) -> datetime | None: ...


def effective_start(project: str, start: datetime | None) -> datetime | None:
    """Apply the selected desk's earliest supported creation date to the UTC window."""
    start = as_utc(start) if start is not None else None
    desk = BY_KEY.get(project)
    cutoff = as_utc(desk.earliest_created) if desk and desk.earliest_created else None
    return max(start, cutoff) if start and cutoff else start or cutoff


def filter_companies(frame: pd.DataFrame, labels: Iterable[str]) -> pd.DataFrame:
    """Select the companies belonging to the chosen service-desk labels."""
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
