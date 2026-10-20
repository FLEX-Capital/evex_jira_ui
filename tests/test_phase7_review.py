"""Independent-review regressions for exact typed development-clone evidence."""

from typing import Any

import pandas as pd
import pytest
from test_analysis_workflow import _cell
from test_link_enrichment import outward
from test_transformation import issue

from data_transformation import load_issues_Amparex


@pytest.mark.parametrize(
    "links,expected",
    [
        ([outward("EXAX-1", "Relates")], False),
        ([outward("EXAXTRA-1")], False),
        ([outward("EXAX-1")], True),
        ([outward("AXT-1")], True),
        ([{"type": {"name": "Cloners"}, "inwardIssue": {"key": "EXAX-1"}}], False),
        (None, None),
    ],
)
def test_notebook_validation_uses_exact_typed_outward_clones(
    links: list[dict[str, Any]] | None,
    expected: bool | None,
) -> None:
    """Generic links, inbound clones and similarly named projects cannot inflate validation."""
    frame = load_issues_Amparex([issue(key="SDAX-1", links=links)])
    if links is None:
        frame.at[0, "issue_links"] = None
        frame.at[0, "clone_in_project"] = "EXAX"
    frame["resolution_bucket"] = pd.Categorical(
        ["support_todo"], categories=["support_todo"]
    )
    frame["confidence"] = 0.9
    exec(
        _cell("13840be3"), {"labelled": frame, "pd": pd, "display": lambda *args: None}
    )
    actual = frame.cloned_to_dev.iloc[0]
    assert pd.isna(actual) if expected is None else bool(actual) is expected
