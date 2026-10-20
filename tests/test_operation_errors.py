"""Operation reports contain bounded causes, never exception payloads."""

import json
from dataclasses import asdict
from pathlib import Path

import pytest
import requests
from test_sync_service import raw, request


def test_diagnostics_do_not_serialize_payload_or_credentials() -> None:
    """A private message and response body cannot enter a structured cause."""
    from operation_errors import safe_diagnostic

    response = requests.Response()
    response.status_code = 403
    response._content = b"SYNTHETIC_PRIVATE_MARKER"
    result = safe_diagnostic(
        requests.HTTPError("SYNTHETIC_PRIVATE_MARKER", response=response),
        operation_id="run-1",
        stage="links",
        key="SDAX-1",
    )
    encoded = json.dumps(asdict(result))
    assert "SYNTHETIC_PRIVATE_MARKER" not in encoded
    assert result.code == "permission" and result.http_status == 403
    assert result.operation_id == "run-1" and result.key == "SDAX-1"
    assert not result.retryable
    unknown = safe_diagnostic(
        RuntimeError("SYNTHETIC_PRIVATE_MARKER"), operation_id="run-1", stage="fetch"
    )
    assert unknown.code == "unexpected"


def test_saved_import_diagnostic_is_safe(storage: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Per-desk and outer failures persist safe causes without private messages."""
    import desk_sync
    import sync_checkpoints

    def fail(*args: object, **kwargs: object) -> list[dict]:
        raise requests.Timeout("SYNTHETIC_PRIVATE_MARKER")

    monkeypatch.setattr(sync_checkpoints, "fetch_jira_issues", fail)
    report = desk_sync.run_sync(request(projects=["SDAX"]))
    status = (storage.parent / "import_status.json").read_text()
    assert "SYNTHETIC_PRIVATE_MARKER" not in status
    assert report.errors == {"SDAX": "timeout"}
    assert report.diagnostics[0].stage == "fetch" and not report.complete


def test_corrupt_cache_is_failed_read_not_empty(
    storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Corruption stops before fetch and is reported as a failed read."""
    import desk_sync
    import sync_checkpoints

    storage.write_bytes(b"invalid pickle SYNTHETIC_PRIVATE_MARKER")
    monkeypatch.setattr(sync_checkpoints, "fetch_jira_issues", lambda *a, **k: [raw()])
    report = desk_sync.run_sync(request(projects=["SDAX"]))
    assert not report.complete and not report.counts
    assert report.diagnostics[0].code == "read_failed"
    assert "SYNTHETIC_PRIVATE_MARKER" not in json.dumps(report.to_dict())
