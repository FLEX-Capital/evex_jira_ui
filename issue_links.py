"""Pure normalization and type-based selection of Jira issue relationships."""

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

import reporting_policy as reporting


@dataclass(frozen=True)
class IssueLink:
    """A typed, directed relationship to one issue key."""

    type_name: str
    direction: Literal["inward", "outward"]
    target_key: str


def parse_issue_links(links: Sequence[object] | None) -> tuple[IssueLink, ...]:
    """Keep valid directed relationships in first-appearance order.

    Malformed records are ignored. Type identity is case insensitive, while
    direction and target identity remain part of each deduplication key.
    """
    if not isinstance(links, (list, tuple)):
        return ()
    result = []
    seen = set()
    directions: tuple[Literal["inward", "outward"], ...] = ("inward", "outward")
    for link in links:
        if not isinstance(link, Mapping):
            continue
        kind = link.get("type")
        name = kind.get("name") if isinstance(kind, Mapping) else None
        if not isinstance(name, str) or not name.strip():
            continue
        name = name.strip()
        for direction in directions:
            target = link.get(direction + "Issue")
            key = target.get("key") if isinstance(target, Mapping) else None
            if not isinstance(key, str) or not key.strip():
                continue
            key = key.strip()
            identity = (name.casefold(), direction, key)
            if identity not in seen:
                result.append(IssueLink(name, direction, key))
                seen.add(identity)
    return tuple(result)


def clone_links(
    links: Sequence[IssueLink],
    *,
    type_names: Collection[str] = reporting.CLONE_TYPE_NAMES,
) -> tuple[IssueLink, ...]:
    """Select clone relationships using the configured case-insensitive types."""
    allowed = {name.casefold() for name in type_names}
    return tuple(link for link in links if link.type_name.casefold() in allowed)
