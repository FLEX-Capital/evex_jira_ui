"""Pure normalization and type-based selection of Jira issue relationships."""

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

import reporting_policy as reporting
from http_policy import InvalidSearchPayload
from operation_errors import OperationDiagnostic, safe_diagnostic


@dataclass(frozen=True)
class IssueLink:
    """A typed, directed relationship to one issue key."""

    type_name: str
    direction: Literal["inward", "outward"]
    target_key: str


@dataclass(frozen=True)
class LinkGraph:
    """Known outgoing records and explicit causes for unavailable first hops."""

    by_key: Mapping[str, tuple[IssueLink, ...]]
    diagnostics: tuple[OperationDiagnostic, ...]


def fetch_link_graph(
    issues: Sequence[Mapping[str, Any]],
    *,
    fetch_many: Callable[[Sequence[str]], Mapping[str, Mapping[str, Any]]],
    operation_id: str,
) -> LinkGraph:
    """Fetch distinct missing outward targets once, in batches of at most 100.

    Args:
        issues: Supplied source records, reused without fetching them again.
        fetch_many: Selected-field adapter returning fields by ticket key.
        operation_id: Generated import identifier for payload-free diagnostics.

    Returns:
        Known direct and first-hop records; second-hop targets are never crawled.
    """
    known = {
        issue["key"]: parse_issue_links(
            (issue.get("fields") or {}).get("issuelinks"), deduplicate=False
        )
        for issue in issues
        if isinstance((issue.get("fields") or {}).get("issuelinks"), list)
    }
    missing = sorted(
        {
            link.target_key
            for links in known.values()
            for link in links
            if link.direction == "outward"
        }
        - known.keys()
    )
    diagnostics: list[OperationDiagnostic] = []
    for offset in range(0, len(missing), 100):
        batch = missing[offset : offset + 100]
        try:
            result = fetch_many(batch)
            if not isinstance(result, Mapping):
                raise InvalidSearchPayload("invalid_link_payload")
        except Exception as exc:  # noqa: BLE001 - optional bounded batch isolation
            diagnostics.extend(
                safe_diagnostic(exc, operation_id=operation_id, stage="links", key=key)
                for key in batch
            )
            continue
        for key in batch:
            fields = result.get(key)
            if not isinstance(fields, Mapping) or not isinstance(
                fields.get("issuelinks"), list
            ):
                diagnostics.append(
                    OperationDiagnostic(
                        operation_id,
                        "links",
                        "not_found" if fields is None else "invalid_payload",
                        key=key,
                    )
                )
            else:
                known[key] = parse_issue_links(fields["issuelinks"], deduplicate=False)
    return LinkGraph(known, tuple(diagnostics))


def parse_issue_links(
    links: Sequence[object] | None, *, deduplicate: bool = True
) -> tuple[IssueLink, ...]:
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
            if not deduplicate or identity not in seen:
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
