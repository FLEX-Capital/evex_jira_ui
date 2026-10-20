"""Refresh missing ticket origins without restricting ticket creation dates."""

import re
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import pandas as pd
import requests

import service_desks as desks
from http_policy import SOURCE, InvalidSearchPayload, validate_search_page, with_retries
from settings import Settings, load_settings


def fetch_issue_fields(
    keys: Sequence[str],
    fields: Sequence[str],
    *,
    settings: Settings | None = None,
    now: Callable[[], datetime] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> dict[str, dict[str, Any]]:
    """Batch-read selected fields; omitted issues/fields stay distinguishable."""
    keys = list(dict.fromkeys(keys))
    if not keys:
        return {}
    if any(not re.fullmatch(r"[A-Z][A-Z0-9_]*-\d+", key) for key in keys):
        raise ValueError("Invalid Jira ticket key in the cache.")

    connection = (settings or load_settings()).require_jira()
    base = connection.url

    results = {}
    with requests.Session() as session:
        session.auth = (connection.username, connection.password)
        for offset in range(0, len(keys), 100):
            batch = keys[offset : offset + 100]
            payload = {
                "jql": f"key in ({','.join(batch)}) ORDER BY key",
                "fields": fields,
                "maxResults": 100,
            }
            tokens = set()
            while True:

                def read_page() -> dict[str, Any]:
                    response = session.post(
                        f"{base}/rest/api/3/search/jql", json=payload, timeout=30
                    )
                    response.raise_for_status()
                    try:
                        return validate_search_page(response.json())
                    except ValueError:
                        raise InvalidSearchPayload("invalid_search_payload") from None

                page = with_retries(
                    read_page,
                    policy=SOURCE,
                    now=now or (lambda: datetime.now(UTC)),
                    sleep=sleep or time.sleep,
                )
                for issue in page["issues"]:
                    results[issue["key"]] = issue.get("fields") or {}
                token = page.get("nextPageToken")
                if not token:
                    break
                if token in tokens:
                    raise InvalidSearchPayload("repeated_pagination_token")
                tokens.add(token)
                payload["nextPageToken"] = token
    return results


def fetch_issue_sources(keys: Sequence[str], *, settings: Settings | None = None) -> dict[str, Any]:
    """Read just the source field, leaving absent/inaccessible issues untouched."""
    return {
        key: (fields[desks.FIELDS.source] or {}).get("value", "")
        for key, fields in fetch_issue_fields(
            keys, [desks.FIELDS.source], settings=settings
        ).items()
        if desks.FIELDS.source in fields
    }


def refresh_missing_sources(
    df: pd.DataFrame,
    fetch_sources: Callable[[Sequence[str]], Mapping[str, Any]] = fetch_issue_sources,
) -> tuple[pd.DataFrame, int]:
    """Fill cached blanks from Jira, preserving existing values and other fields."""
    result = df.copy()
    if result.empty:
        return result, 0
    missing = result["source"].astype("string").fillna("").str.strip().eq("")
    if not missing.any():
        return result, 0
    sources = fetch_sources(result.loc[missing, "key"].tolist())
    values = result["key"].map(sources)
    filled = missing & values.astype("string").fillna("").str.strip().ne("")
    result.loc[filled, "source"] = values.loc[filled]
    return result, int(filled.sum())
