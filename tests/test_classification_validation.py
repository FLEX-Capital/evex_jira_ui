"""Classifier taxonomy and row isolation keep malformed inputs out of paid work."""

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pandas as pd
import pytest
from classification_env import forbid_model_network as forbid_model_network
from pydantic import ValidationError
from test_ticket_classifier import FakeClient, fake_classification, make_response

import ticket_classifier as classifier

pytestmark = pytest.mark.usefixtures("forbid_model_network")

PAIRS = {
    "question_answer": ("how_to", "where_to_find", "policy_or_process_clarification"),
    "support_todo": (
        "data_correction",
        "document_or_invoice_resend",
        "configuration_change",
        "permissions_or_access",
        "manual_workaround",
    ),
    "team_handoff": (
        "bug",
        "feature_request",
        "database_fix",
        "hardware_or_infrastructure",
        "external_vendor",
    ),
    "unclear": ("insufficient_information",),
}


def _result(**changes: Any) -> dict[str, Any]:
    result = dict(
        reasoning="Synthetic",
        resolution_bucket="question_answer",
        sub_bucket="how_to",
        intent_label="hilfe finden",
        ai_mode="human_only",
        confidence=0.9,
    )
    result.update(changes)
    return result


@pytest.mark.parametrize(
    "parent,child", [(p, c) for p, children in PAIRS.items() for c in children]
)
def test_every_existing_taxonomy_pair_round_trips(parent: str, child: str) -> None:
    """Every established category pair and AI mode remains valid."""
    for mode in classifier.AI_MODES:
        values = _result(resolution_bucket=parent, sub_bucket=child, ai_mode=mode)
        parsed = classifier.TicketClassification.model_validate(values)
        assert parsed.model_dump() == values


@pytest.mark.parametrize(
    "changes",
    [
        {"resolution_bucket": "invented"},
        {"sub_bucket": "invented"},
        {"sub_bucket": "bug"},
        {"ai_mode": "invented"},
        {"confidence": -0.1},
        {"confidence": 1.1},
        {"extra": "SYNTHETIC_PRIVATE_MARKER"},
    ],
)
def test_taxonomy_rejects_unknown_and_cross_parent_values(
    changes: dict[str, Any],
) -> None:
    """Unknown, mismatched, out-of-range and extra output values are rejected."""
    with pytest.raises(ValidationError):
        classifier.TicketClassification.model_validate(_result(**changes))


def test_taxonomy_schema_constrains_model_choices() -> None:
    """The emitted structured-output schema contains actual enum constraints."""
    schema = classifier.TicketClassification.model_json_schema()
    for field, values in (
        ("resolution_bucket", classifier.RESOLUTION_BUCKETS),
        ("sub_bucket", classifier.SUB_BUCKETS),
        ("ai_mode", classifier.AI_MODES),
    ):
        assert schema["properties"][field]["enum"] == list(values)
    assert schema["additionalProperties"] is False


@pytest.mark.parametrize("missing", [None, pd.NA, float("nan"), pd.NaT, "", [], {}, (), set()])
def test_missing_scalars_render_without_ambiguous_truth(missing: object) -> None:
    """Nullable scalars and empty optional containers render without boolean coercion."""
    assert classifier.scalar_text(missing) == ""
    row = {
        "key": "SDAX-1",
        "summary": "First",
        "description": missing,
        "comments": missing,
        "Hauptkategorie": missing,
        "Unterkategorie": missing,
    }
    key, text = classifier.validate_ticket_input(row)
    assert key == "SDAX-1" and text == "Summary: First"
    long = classifier.build_ticket_text({**row, "comments": "x" * 10000}, max_chars=500)
    assert long.startswith("Summary: First") and long.endswith("\n[...truncated]")
    assert len(long) == 500 + len("\n[...truncated]")


def test_one_bad_record_does_not_abort_valid_batch(tmp_path: Path) -> None:
    """Invalid keys/text stay isolated and only two valid rows reach the fake classifier."""
    values = [
        {"key": "SDAX-1", "summary": "First", "description": pd.NA},
        {"key": " ", "summary": "Invalid key"},
        {"key": None, "summary": "Missing key"},
        {"key": "SDAX-4", "summary": "", "description": None},
        {
            "key": "SDAX-5",
            "summary": "Valid",
            "description": {"private": "SYNTHETIC_PRIVATE_MARKER"},
        },
        {"key": "SDAX-6", "summary": "Second", "comments": "Resolved"},
    ]
    cache = tmp_path / "classifier.parquet"
    with patch.object(classifier, "classify_one", side_effect=fake_classification) as call:
        result = classifier.classify_tickets(
            pd.DataFrame(values),
            cache_path=cache,
            model="fake",
            client=object(),
            max_workers=1,
            progress=False,
        )
    assert call.call_count == 2 and set(result.loc[result.error.isna(), "key"]) == {
        "SDAX-1",
        "SDAX-6",
    }
    assert set(result.loc[result.error.notna(), "key"]) == {"SDAX-4", "SDAX-5"}
    assert result.attrs["classification_errors"] == [
        {"index": 1, "code": "invalid_key"},
        {"index": 2, "code": "invalid_key"},
        {"index": 3, "code": "empty_text"},
        {"index": 4, "code": "invalid_text"},
    ]
    assert "SYNTHETIC_PRIVATE_MARKER" not in json.dumps(result.attrs)
    assert set(classifier.load_cache(cache).key) == {"SDAX-1", "SDAX-6"}


@pytest.mark.parametrize("kind", ["exception", "refusal", "invalid_output"])
def test_sdk_errors_do_not_persist_ticket_or_secret_text(kind: str) -> None:
    """Exception bodies, refusal text and invalid model payloads become bounded codes."""
    if kind == "exception":
        response = RuntimeError("SYNTHETIC_PRIVATE_MARKER")
    elif kind == "refusal":
        response = make_response(refusal="SYNTHETIC_PRIVATE_MARKER")
    else:
        response = make_response(parsed=_result(resolution_bucket="SYNTHETIC_PRIVATE_MARKER"))
    result = classifier.classify_one(
        FakeClient(response), "SDAX-1", "Summary: synthetic", model="fake"
    )
    assert result["error"] in {"unexpected", "refusal", "invalid_output"}
    assert "SYNTHETIC_PRIVATE_MARKER" not in json.dumps(result)
    assert result["reasoning"] is None
