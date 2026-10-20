"""Shared retries respect server delays and search payloads remain explicit."""

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests
from test_sync_service import raw, request

import category_sync
import data_loading
import desk_sync
import jira_loader
import source_sync
import sync_checkpoints
from settings import load_settings

NOW = datetime(2026, 10, 20, 12, tzinfo=UTC)


def _http_error(status: int, delay: str = "120") -> requests.HTTPError:
    response = requests.Response()
    response.status_code = status
    response.headers["Retry-After"] = delay
    return requests.HTTPError("SYNTHETIC_PRIVATE_MARKER", response=response)


@pytest.mark.parametrize("header", ["120", "Tue, 20 Oct 2026 12:02:00 GMT"])
def test_retry_after_numeric_date_and_falsey_response(header: str) -> None:
    """A falsey 429 response still supplies its full two-minute wait."""
    from http_policy import SOURCE, retry_after_delay, with_retries

    assert retry_after_delay({"Retry-After": header}, 2, now=NOW) == 120
    error = _http_error(429, header)
    assert not error.response
    operation = Mock(side_effect=[error, "success"])
    delays: list[float] = []
    assert (
        with_retries(operation, policy=SOURCE, now=lambda: NOW, sleep=delays.append)
        == "success"
    )
    assert operation.call_count == 2 and delays == [120]


@pytest.mark.parametrize(
    "header,expected",
    [("bad", 2), ("nan", 2), ("inf", 2), ("Tue, 20 Oct 2026 11:59:00 GMT", 0)],
)
def test_retry_after_malformed_and_past_values(header: str, expected: float) -> None:
    """Invalid/nonfinite headers use fallback; a past HTTP date permits zero."""
    from http_policy import retry_after_delay

    assert retry_after_delay({"Retry-After": header}, 2, now=NOW) == expected


def test_excess_delay_defers_and_permanent_errors_do_not_retry() -> None:
    """Over-budget waits defer, while permanent errors fail on their first try."""
    from http_policy import CATEGORY, InvalidSearchPayload, RetryDeferred, with_retries

    delays: list[float] = []
    operation = Mock(side_effect=_http_error(429, "301"))
    with pytest.raises(RetryDeferred) as caught:
        with_retries(operation, policy=CATEGORY, now=lambda: NOW, sleep=delays.append)
    assert caught.value.delay == 301 and not delays and operation.call_count == 1
    for error in (
        _http_error(403),
        _http_error(404),
        ValueError("invalid identity"),
        InvalidSearchPayload("invalid_payload"),
    ):
        operation = Mock(side_effect=error)
        with pytest.raises(type(error)):
            with_retries(
                operation, policy=CATEGORY, now=lambda: NOW, sleep=delays.append
            )
        assert operation.call_count == 1 and not delays


@pytest.mark.parametrize(
    "policy_name,attempts,expected_delays",
    [("CATEGORY", 3, [1, 2]), ("SOURCE", 3, [1, 2]), ("COUNTRY", 6, [2, 4, 8, 16, 32])],
)
@pytest.mark.parametrize(
    "error",
    [
        requests.Timeout("synthetic"),
        requests.ConnectionError("synthetic"),
        _http_error(503, "bad"),
    ],
)
def test_operation_budgets_stay_distinct(
    policy_name: str, attempts: int, expected_delays: list[float], error: Exception
) -> None:
    """Transport/5xx retries consume each operation's own documented budget."""
    import http_policy

    operation = Mock(side_effect=error)
    delays: list[float] = []
    with pytest.raises(type(error)):
        http_policy.with_retries(
            operation,
            policy=getattr(http_policy, policy_name),
            now=lambda: NOW,
            sleep=delays.append,
        )
    assert operation.call_count == attempts and delays == expected_delays


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"errorMessages": ["private"]},
        {"issues": [], "errorMessages": ["private"]},
        {"issues": {}},
        {"issues": [None]},
        {"issues": [{"key": ""}]},
        {"issues": [{"key": "SDAX-1", "fields": []}]},
    ],
)
def test_success_payload_requires_issue_array(payload: object) -> None:
    """Error or malformed success pages never turn into successful empty searches."""
    from http_policy import InvalidSearchPayload, validate_search_page

    with pytest.raises(InvalidSearchPayload) as caught:
        validate_search_page(payload)
    assert "private" not in str(caught.value)
    assert validate_search_page({"issues": []}) == {"issues": []}


def test_category_deferred_wait_and_http_date_use_shared_policy() -> None:
    """Category identity validation remains outside transient transport retry."""
    config = load_settings({})
    delays: list[float] = []
    fetch = Mock(
        side_effect=[
            _http_error(429, "Tue, 20 Oct 2026 12:02:00 GMT"),
            {"id": "41", "label": "Resolved"},
        ]
    )
    resolver = category_sync.CategoryResolver(
        fetch=fetch, sleep=delays.append, now=lambda: NOW
    )
    value = resolver.resolve(config.tenant.cloud_id, config.tenant.workspace_id, "41")
    assert value.label == "Resolved" and value.attempts == 2 and delays == [120]
    fetch = Mock(side_effect=_http_error(429, "301"))
    resolver = category_sync.CategoryResolver(
        fetch=fetch, sleep=delays.append, now=lambda: NOW
    )
    value = resolver.resolve(config.tenant.cloud_id, config.tenant.workspace_id, "41")
    assert value.label is None and value.error == "deferred" and value.attempts == 1
    assert fetch.call_count == 1 and delays == [120]


def _session(monkeypatch: pytest.MonkeyPatch, *pages: object) -> Mock:
    session = Mock()
    session.__enter__ = Mock(return_value=session)
    session.__exit__ = Mock(return_value=False)
    responses = [Mock(json=Mock(return_value=page)) for page in pages]
    session.post.side_effect = responses
    monkeypatch.setattr(source_sync.requests, "Session", Mock(return_value=session))
    for name, value in {
        "JIRA_URL": "https://jira.example",
        "JIRA_USERNAME": "test",
        "JIRA_PASSWORD": "synthetic",
    }.items():
        monkeypatch.setenv(name, value)
    return session


def test_source_repair_reports_malformed_search(
    storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 200 error payload makes source repair incomplete, preserving valid rows."""
    value = raw()
    value["fields"]["customfield_10675"] = {"value": ""}
    monkeypatch.setattr(sync_checkpoints, "fetch_jira_issues", lambda *a, **k: [value])
    _session(monkeypatch, {"errorMessages": ["SYNTHETIC_PRIVATE_MARKER"]})
    report = desk_sync.run_sync(request(projects=["SDAX"], repair_sources=True))
    assert not report.complete and report.source_error and report.committed
    assert data_loading.load_data(path=storage).source.tolist() == [""]


def test_source_search_keeps_omitted_keys_distinct_and_rejects_repeated_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only returned field values may patch a cache; repeated tokens fail."""
    from http_policy import InvalidSearchPayload

    _session(
        monkeypatch,
        {
            "issues": [
                {"key": "SDAX-1", "fields": {"customfield_10675": {"value": "Portal"}}}
            ]
        },
    )
    assert source_sync.fetch_issue_sources(["SDAX-1", "SDAX-2"]) == {"SDAX-1": "Portal"}
    _session(
        monkeypatch,
        {"issues": [], "nextPageToken": "same"},
        {"issues": [], "nextPageToken": "same"},
    )
    with pytest.raises((InvalidSearchPayload, RuntimeError), match="pagination|token"):
        source_sync.fetch_issue_fields(["SDAX-1"], ["summary"])


def test_raw_fetch_rejects_malformed_issue_elements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Jira SDK search results receive the same validation before transformation."""
    from http_policy import InvalidSearchPayload

    config = load_settings(
        {
            "JIRA_URL": "https://jira.example",
            "JIRA_USERNAME": "test",
            "JIRA_PASSWORD": "synthetic",
        }
    )
    client = Mock()
    client.myself.return_value = {"timeZone": "UTC"}
    client.enhanced_search_issues.return_value = {"issues": [{"fields": {}}]}
    monkeypatch.setattr(jira_loader, "JIRA", Mock(return_value=client))
    with pytest.raises(InvalidSearchPayload):
        jira_loader.fetch_jira_issues(project="SDAX", save_path=None, settings=config)


def test_raw_sdk_fetch_uses_one_explicit_retry_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SDK retries cannot multiply the shared read budget or bypass injected waits."""
    config = load_settings(
        {
            "JIRA_URL": "https://jira.example",
            "JIRA_USERNAME": "test",
            "JIRA_PASSWORD": "synthetic",
        }
    )
    client = Mock()
    client.myself.return_value = {"timeZone": "UTC"}
    client.enhanced_search_issues.side_effect = [
        _http_error(429, "120"),
        {"issues": []},
    ]
    constructor = Mock(return_value=client)
    monkeypatch.setattr(jira_loader, "JIRA", constructor)
    delays: list[float] = []
    assert (
        jira_loader.fetch_jira_issues(
            project="SDAX",
            save_path=None,
            settings=config,
            now=lambda: NOW,
            sleep=delays.append,
        )
        == []
    )
    assert client.enhanced_search_issues.call_count == 2 and delays == [120]
    assert constructor.call_args.kwargs["max_retries"] == 0
    assert constructor.call_args.kwargs["timeout"] == 30
