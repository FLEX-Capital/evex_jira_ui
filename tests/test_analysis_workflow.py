"""Selected analysis cohorts and explicit model work retain exact safe provenance."""

import io
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

import pandas as pd
import pytest
from classification_env import forbid_model_network as forbid_model_network
from test_ticket_classifier import fake_classification, make_frame
from test_transformation import issue

import data_transformation as transform
import jira_loader
import ticket_classifier as classifier
from classification_cache import configuration_fingerprint, merge_classifications
from settings import load_settings

pytestmark = pytest.mark.usefixtures("forbid_model_network")
NOTEBOOK = Path(__file__).resolve().parents[1] / "amparex_ai_assist_analysis.ipynb"


def _cell(identifier: str) -> str:
    notebook = json.loads(NOTEBOOK.read_text())
    return "".join(
        next(cell for cell in notebook["cells"] if cell["id"] == identifier)["source"]
    )


@pytest.mark.parametrize(
    "day,late",
    [
        (date(2026, 3, 29), "2026-03-29T21:59:59.999999Z"),
        (date(2026, 10, 25), "2026-10-25T22:59:59.999999Z"),
    ],
)
def test_project_and_inclusive_window_apply_after_cache_load(
    day: date, late: str
) -> None:
    """Include late tickets on both DST days; excluded rows leave identity stable."""
    from analysis_workflow import AnalysisSpec, cohort_digest, select_cohort

    spec = AnalysisSpec("SDAX", day, day, "1")
    base = pd.DataFrame([{"key": "SDAX-1", "created": late, "summary": "Synthetic"}])
    extra = pd.DataFrame(
        [
            {"key": "SDAX-2", "created": "2020-01-01"},
            {"key": "SDAX-3", "created": "2030-01-01"},
            {"key": "SDAX-4", "created": "bad"},
            {"key": "SDAXA-5", "created": late},
            {"key": "SDEU-6", "created": late},
        ]
    )
    first = select_cohort(base, spec)
    selected = select_cohort(pd.concat([base, extra], ignore_index=True), spec)
    assert selected.key.tolist() == ["SDAX-1"]
    assert cohort_digest(selected) == cohort_digest(first)
    assert selected.attrs["analysis_selection"] == {
        "selected_rows": 1,
        "invalid_created_rows": 1,
        "excluded_window_rows": 2,
        "excluded_project_rows": 2,
    }
    assert len(base) == 1 and len(extra) == 5


def test_modern_unresolved_categories_do_not_borrow_static_labels() -> None:
    """Notebook preparation cannot undo normal-workspace provenance or legacy-only fallback."""
    from analysis_workflow import AnalysisSpec, select_cohort

    modern = issue(
        key="SDAX-1",
        main_id="41",
        asset_labels={},
        assets_workspace_id=jira_loader.WORKSPACE_ID,
        assets_cloud_id=jira_loader.CLOUD_ID,
    )
    foreign = issue(
        key="SDAX-2",
        main_id="41",
        asset_labels={"41": "Wrong workspace"},
        assets_workspace_id=jira_loader.WORKSPACE_ID,
        assets_cloud_id=jira_loader.CLOUD_ID,
    )
    foreign["fields"]["customfield_10680"][0]["workspaceId"] = "foreign"
    legacy = issue(key="SDAX-3", main_id="41")
    with patch.dict(transform.object_id_to_name, {"41": "Legacy category"}):
        frame = transform.load_issues_Amparex([modern, foreign, legacy])
    selected = select_cohort(
        frame, AnalysisSpec("SDAX", date(2026, 9, 1), date(2026, 9, 10), "1")
    )
    before = selected.copy(deep=True)
    code = _cell("69e06888")
    with patch(
        "builtins.open", return_value=io.StringIO('{"41":"Forbidden static label"}')
    ):
        exec(compile(code, str(NOTEBOOK), "exec"), {"df": selected})
    assert selected.Hauptkategorie.tolist() == [
        "Unbekannt",
        "Unbekannt",
        "Legacy category",
    ]
    pd.testing.assert_frame_equal(selected, before)


def test_manifest_identifies_exact_cohort_and_configuration() -> None:
    """Keep selected-input identity separate from source-artifact identity."""
    from analysis_workflow import (
        AnalysisSpec,
        analysis_manifest,
        cohort_digest,
        select_cohort,
    )

    spec = AnalysisSpec("SDAX", date(2026, 9, 1), date(2026, 9, 10), "1")
    frame = make_frame(["SDAX-2", "SDAX-1"]).assign(created="2026-09-02T10:00:00Z")
    selected = select_cohort(frame, spec)
    stamp = datetime(2026, 10, 20, tzinfo=UTC)
    first = analysis_manifest(
        selected,
        spec,
        extracted_at=stamp,
        extraction_digest="a" * 64,
        classifier_fingerprint="b" * 64,
    )
    reordered = analysis_manifest(
        selected.iloc[::-1],
        spec,
        extracted_at=stamp,
        extraction_digest="a" * 64,
        classifier_fingerprint="b" * 64,
    )
    assert first == reordered and first["selected_rows"] == 2
    assert first["project"] == "SDAX" and first["classification_policy_version"] == "1"
    assert "Synthetic" not in json.dumps(first) and "Rechnung" not in json.dumps(first)
    changed_source = analysis_manifest(
        selected,
        spec,
        extracted_at=stamp,
        extraction_digest="c" * 64,
        classifier_fingerprint="b" * 64,
    )
    assert changed_source["cohort_digest"] == first["cohort_digest"]
    assert changed_source["extraction_digest"] != first["extraction_digest"]
    assert cohort_digest(selected.assign(summary="changed")) != cohort_digest(selected)
    assert cohort_digest(selected.assign(Hauptkategorie="changed")) != cohort_digest(
        selected
    )


@pytest.mark.parametrize("run", [False, True])
def test_classification_opt_in_and_one_to_one_join(tmp_path: Path, run: bool) -> None:
    """Default to cache-only; explicit work uses the captured config and strict joins."""
    path = tmp_path / "classifications.parquet"
    config = classifier.classifier_configuration(load_settings({}), model="fake")
    frame = make_frame(["SDAX-1"])
    merge_classifications(
        path, pd.DataFrame([fake_classification(None, "SDAX-99", "old")])
    )

    def fake(selected: pd.DataFrame, **kwargs: Any) -> pd.DataFrame:
        assert selected.key.tolist() == ["SDAX-1"] and kwargs["configuration"] == config
        with patch.object(classifier, "classify_one", side_effect=fake_classification):
            return classifier.classify_tickets(
                selected, client=object(), progress=False, **kwargs
            )

    runner = Mock(side_effect=fake)
    values: dict[str, Any] = {
        "df": frame,
        "pd": pd,
        "CACHE_PATH": path,
        "classification_path": path,
        "RUN_CLASSIFICATION": run,
        "configuration": config,
        "classification_runner": runner,
        "classify_tickets": classifier.classify_tickets,
    }
    with patch.object(
        classifier, "build_client", side_effect=AssertionError("automatic paid work")
    ):
        exec(compile(_cell("ba40e8a4"), str(NOTEBOOK), "exec"), values)
    assert runner.call_count == int(run)
    results = values["results"]
    assert results.key.tolist() == (["SDAX-1"] if run else [])
    if run:
        assert results.iloc[0].config_digest == configuration_fingerprint(config)
        values["results"] = pd.concat([results, results], ignore_index=True)
        with pytest.raises(pd.errors.MergeError):
            exec(compile(_cell("7e4f7b4d"), str(NOTEBOOK), "exec"), values)
