"""Desk identity and import boundaries, independent of Jira credentials."""

from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo


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


def filter_issue_window(project, issues, start, end):
    """Enforce exact instants after Jira's minute-granularity search/checkpoints."""
    start = effective_start(project, start)
    end = as_utc(end) if end else None
    result = []
    for issue in issues:
        if not issue.get("key", "").startswith(project + "-"):
            continue
        created = datetime.fromisoformat(issue["fields"]["created"])
        created = as_utc(created)
        if (start is None or created >= start) and (end is None or created <= end):
            result.append(issue)
    return result
