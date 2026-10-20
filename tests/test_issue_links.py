"""Normalized relationships distinguish clone links from generic issue links."""

from pathlib import Path
from unittest.mock import patch

from test_transformation import issue


def test_clone_filter_ignores_blocks_and_keeps_both_directions() -> None:
    """Clone records retain directions and casefold type identity."""
    from issue_links import clone_links, parse_issue_links

    links = parse_issue_links(
        [
            {"type": {"name": "Blocks"}, "outwardIssue": {"key": "AX-1"}},
            {"type": {"name": "cLoNeRs"}, "inwardIssue": {"key": "AX-2"}},
            {"type": {"name": "Cloners"}, "outwardIssue": {"key": "AX-3"}},
            {"type": {"name": "CLONERS"}, "inwardIssue": {"key": "AX-2"}},
            {"type": {"name": "Cloners"}, "outwardIssue": {"key": "AX-2"}},
        ]
    )
    assert [(x.direction, x.target_key) for x in clone_links(links)] == [
        ("inward", "AX-2"),
        ("outward", "AX-3"),
        ("outward", "AX-2"),
    ]
    assert [x.target_key for x in clone_links(links, type_names=("Blocks",))] == [
        "AX-1"
    ]


def test_invalid_relationships_are_ignored_without_crashing() -> None:
    """Malformed neighbors cannot fabricate links or hide valid directions."""
    from issue_links import clone_links, parse_issue_links

    invalid: list[object] = [
        None,
        [],
        4,
        {},
        {"type": None},
        {"type": {"name": ""}},
        {"type": {"name": 3}, "inwardIssue": {"key": "AX-1"}},
        {"type": {"name": "Cloners"}, "inwardIssue": {"key": None}},
        {"type": {"name": "Cloners"}, "outwardIssue": "AX-1"},
    ]
    valid = {"type": {"name": "Cloners"}, "outwardIssue": {"key": "AX-2"}}
    parsed = clone_links(parse_issue_links([*invalid, valid]))
    assert len(parsed) == 1 and parsed[0].target_key == "AX-2"
    assert parse_issue_links(None) == ()


def test_legacy_generic_fields_keep_their_contract(storage: Path) -> None:
    """Normalized clone counts do not rewrite legacy generic link semantics."""
    import data_transformation as transform

    links = [
        {"type": {"name": "Blocks"}, "outwardIssue": {"key": "EXAX-1"}},
        {"type": {"name": "Cloners"}, "inwardIssue": {"key": "SOURCE-1"}},
        {"type": {"name": "Cloners"}, "outwardIssue": {"key": "SOURCE-1"}},
        {"type": {"name": "Cloners"}, "outwardIssue": {"key": "AX-2"}},
    ]
    with patch.object(transform, "load_clone_of_clone", return_value=("", "")):
        row = transform.load_issues_Euronet([issue(links=links)]).iloc[0]
    assert row.clones == "EXAX-1,SOURCE-1,AX-2"
    assert row.clone_types == "Blocks,Cloners,Cloners"
    assert row.n_clones == 3 and row.cloned_by == ""
    assert row.clone_count == 2
    assert row.clone_inward_keys == ["SOURCE-1"]
    assert row.clone_outward_keys == ["SOURCE-1", "AX-2"]
    assert row.issue_links[0] == {
        "type_name": "Blocks",
        "direction": "outward",
        "target_key": "EXAX-1",
    }


def test_legacy_clone_data_is_excluded_in_view(public_ui: None) -> None:
    """Old generic relationship text is explicitly excluded from clone charts."""
    import pandas as pd
    from streamlit.testing.v1 import AppTest

    from data_transformation import load_issues_Euronet

    frame = load_issues_Euronet([issue(main_id=None, sub_id=None)])
    frame = frame.drop(
        columns=[
            "issue_links",
            "clone_count",
            "clone_inward_keys",
            "clone_outward_keys",
        ]
    )
    with (
        patch("data_loading.load_data", return_value=frame),
        patch("interactive.render_interactive"),
    ):
        app = AppTest.from_file("app.py", default_timeout=30).run()
        app.sidebar.date_input[0].set_value(
            (pd.Timestamp("2026-09-01").date(), pd.Timestamp("2026-09-10").date())
        ).run()
    assert not app.exception
    assert any(
        "1 Tickets" in c.value and "Beziehungsdaten" in c.value for c in app.caption
    )
