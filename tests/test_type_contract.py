"""The complete maintained type scope rejects invalid contracts without hiding production debt."""

import ast
import fnmatch
import os
import re
import subprocess
import sys
import tomllib
from collections.abc import Callable
from pathlib import Path

import pytest
from test_python_quality import quality_repo as quality_repo

ROOT = Path(__file__).resolve().parents[1]


def _maintained() -> set[Path]:
    return {p for p in ROOT.glob("*.py") if p.name != "build_board_deck.py"} | set(
        (ROOT / "tools").glob("*.py")
    )


def test_type_scope_includes_all_runtime_and_classifier_contracts() -> None:
    """Declared scope includes every application/tool API without a broad import ignore."""
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["mypy"]
    selected = set()
    for pattern in config["files"]:
        paths = [ROOT] if pattern == "." else ROOT.glob(pattern)
        for path in paths:
            candidates = (
                {p for p in _maintained() if p.is_relative_to(path)} if path.is_dir() else {path}
            )
            selected.update(
                p
                for p in candidates
                if not re.search(config.get("exclude", r"a^"), str(p.relative_to(ROOT)))
            )
    assert _maintained() <= selected
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    assert "--filter=-mypy" not in workflow
    assert "uv run --frozen --no-sync mypy" in workflow
    assert config["disallow_untyped_defs"] and not config.get("ignore_missing_imports", False)
    for override in config.get("overrides", []):
        if override.get("ignore_errors") or override.get("ignore_missing_imports"):
            modules = override["module"]
            modules = [modules] if isinstance(modules, str) else modules
            assert not any(
                fnmatch.fnmatch(path.stem, module) for module in modules for path in _maintained()
            )
    for path in _maintained():
        source = path.read_text()
        assert not re.search(r"#\s*mypy:\s*ignore-errors|@no_type_check", source), path.name


@pytest.fixture
def typecheck_fixture(tmp_path: Path) -> Callable[[str], subprocess.CompletedProcess[str]]:
    """Check temporary calls against real signatures without executing application code."""

    def check(source: str) -> subprocess.CompletedProcess[str]:
        path = tmp_path / "contract_probe.py"
        path.write_text(source)
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "mypy",
                "--follow-imports=silent",
                "--show-error-codes",
                "--no-incremental",
                str(path),
            ],
            cwd=ROOT,
            env=dict(os.environ, MYPYPATH=str(ROOT)),
            capture_output=True,
            text=True,
            timeout=120,
        )

    return check


def test_negative_type_fixture_is_rejected(
    typecheck_fixture: Callable[[str], subprocess.CompletedProcess[str]],
) -> None:
    """Wrong dates fail by argument type while the correct immutable-request call passes."""
    invalid = typecheck_fixture(
        "from desk_sync import make_sync_request\nmake_sync_request(123, 456)\n"
    )
    assert invalid.returncode != 0 and "arg-type" in invalid.stdout, invalid.stdout + invalid.stderr
    valid = typecheck_fixture(
        "from datetime import UTC, datetime\nfrom desk_sync import make_sync_request\n"
        "make_sync_request(datetime(2026,1,1,tzinfo=UTC), "
        "datetime(2026,1,2,tzinfo=UTC), ['SDAX'])\n"
    )
    assert valid.returncode == 0, valid.stdout + valid.stderr


def test_public_interface_documentation_and_existing_behavior() -> None:
    """Maintained production APIs have complete signatures/docs, including variadic parameters."""
    missing = []
    for path in _maintained():
        tree = ast.parse(path.read_text())
        parents = {
            child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)
        }
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = node.args.posonlyargs + node.args.args + node.args.kwonlyargs
                args += [arg for arg in (node.args.vararg, node.args.kwarg) if arg is not None]
                if node.returns is None or any(
                    arg.annotation is None and arg.arg not in {"self", "cls"} for arg in args
                ):
                    missing.append(f"{path.name}:{node.lineno}:{node.name}:signature")
                if (
                    isinstance(parents.get(node), (ast.Module, ast.ClassDef))
                    and not node.name.startswith("_")
                    and ast.get_docstring(node) is None
                    and not any(
                        isinstance(d, ast.Name) and d.id == "overload" for d in node.decorator_list
                    )
                ):
                    missing.append(f"{path.name}:{node.lineno}:{node.name}:docstring")
            if (
                isinstance(node, ast.ClassDef)
                and not node.name.startswith("_")
                and ast.get_docstring(node) is None
            ):
                missing.append(f"{path.name}:{node.lineno}:{node.name}:docstring")
    assert not missing, missing


def test_native_type_gate_uses_configured_scope(
    quality_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The same index-capable gate runs real mypy and returns its failing assignment diagnostic."""
    from tools.check_python import main

    config = quality_repo / "pyproject.toml"
    config.write_text(config.read_text() + '\n[tool.mypy]\nfiles=["*.py"]\n')
    path = quality_repo / "sample.py"
    path.write_text("value: str = 123\n")
    subprocess.run(["git", "-C", str(quality_repo), "add", "."], check=True)
    args = ["--all", "--types", "--repo-root", str(quality_repo)]
    assert main(args) == 1
    assert "assignment" in capsys.readouterr().out
    path.write_text('value: str = "correct"\n')
    assert main(args) == 0


def test_migration_missing_reload_fails_before_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A vanished reload cannot create a migration backup or publish invalid data."""
    from test_cache_migration import _legacy

    import cache_maintenance as maintenance
    from data_loading import CacheChangedError

    path = tmp_path / "jira_data.pkl"
    before = _legacy(path)
    plan = maintenance.plan_migration(path)
    monkeypatch.setattr(maintenance, "load_data", lambda **kwargs: None)
    with pytest.raises(CacheChangedError, match="cache_missing_after_capture"):
        maintenance.apply_migration(plan)
    assert path.read_bytes() == before and not list(tmp_path.glob("*.bak-*"))


def test_empty_successful_category_lookup_is_isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    """A malformed empty success is an unresolved row; valid peers still produce patches."""
    import pandas as pd

    import service_desks
    from category_sync import CategoryLookup, CategoryResolver, prepare_category_repairs
    from settings import load_settings

    config = load_settings({})
    resolver = CategoryResolver(settings=config)
    monkeypatch.setattr(
        resolver,
        "resolve",
        lambda cloud, workspace, oid: (
            CategoryLookup(None, None, 1) if oid == "41" else CategoryLookup("Valid", None, 1)
        ),
    )
    frame = pd.DataFrame(
        [
            {"key": f"SDAX-{i}", "main_category_id": oid, "Hauptkategorie": "Unbekannt"}
            for i, oid in ((1, "41"), (2, "42"))
        ]
    )
    refs = {
        f"SDAX-{i}": {
            service_desks.FIELDS.main_category: [
                {"objectId": oid, "workspaceId": config.tenant.workspace_id}
            ]
        }
        for i, oid in ((1, "41"), (2, "42"))
    }
    patches, report = prepare_category_repairs(frame, references=refs, resolver=resolver)
    assert [(p.key, p.label) for p in patches] == [("SDAX-2", "Valid")]
    assert report.unresolved_cells == 1
    assert list(report.failed_objects.values()) == ["empty_category_label"]


@pytest.mark.parametrize("missing", ["start", "end"])
def test_partial_coverage_requires_both_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    """Inconsistent partial coverage is rejected before fetching or checkpoint publication."""
    from datetime import UTC, datetime
    from unittest.mock import Mock

    from sync_checkpoints import CheckpointStore

    start, end = datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 9, 2, tzinfo=UTC)
    store = CheckpointStore(tmp_path)
    monkeypatch.setattr(
        store,
        "load_partial",
        lambda *args, **kwargs: (
            [{"key": "SDAX-1"}],
            None if missing == "start" else start,
            None if missing == "end" else end,
        ),
    )
    fetch = Mock(side_effect=AssertionError("must not fetch"))
    store.fetch_jira_issues = fetch
    with pytest.raises(ValueError, match="partial_coverage"):
        store.fetch_project("SDAX", start, end, True)
    fetch.assert_not_called()
    assert list(tmp_path.iterdir()) == []


def test_partial_coverage_validation_survives_optimized_python(tmp_path: Path) -> None:
    """The rejection remains active under -O, which removes assertion statements."""
    code = (
        "from datetime import UTC,datetime\nfrom sync_checkpoints import CheckpointStore\n"
        f"store=CheckpointStore({str(tmp_path)!r})\n"
        "start=datetime(2026,9,1,tzinfo=UTC);end=datetime(2026,9,2,tzinfo=UTC)\n"
        "store.load_partial=lambda *a,**k:([{'key':'SDAX-1'}],None,end)\n"
        "try: store.fetch_project('SDAX',start,end,True)\n"
        "except ValueError: print('coverage-rejected')\n"
        "else: raise SystemExit('coverage accepted')\n"
    )
    result = subprocess.run(
        [sys.executable, "-O", "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0 and "coverage-rejected" in result.stdout, result.stderr
