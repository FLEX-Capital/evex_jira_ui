"""Execute a temporary notebook top to bottom against synthetic, offline-only inputs."""

import hashlib
import json
import os
import sys
import tomllib
from pathlib import Path
from typing import Any

import nbformat
import pytest
from jupyter_client import KernelManager
from nbclient import NotebookClient
from test_transformation import issue

from data_loading import save_data
from data_transformation import load_issues_Amparex
from settings import DEFAULT_TENANT

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "amparex_ai_assist_analysis.ipynb"


@pytest.fixture(
    scope="module",
    params=[True, False, "empty"],
    ids=["fake-classification", "cache-only", "empty-cohort"],
)
def notebook_smoke(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> dict[str, Any]:
    """Run each mode once in a child kernel with an isolated cwd, environment and artifacts."""
    run = request.param is True
    empty = request.param == "empty"
    directory = tmp_path_factory.mktemp("notebook")
    data = directory / "data"
    normal = issue(
        key="SDAX-1",
        main_id="41",
        asset_labels={"41": "Known category"},
        assets_workspace_id=DEFAULT_TENANT.workspace_id,
        assets_cloud_id=DEFAULT_TENANT.cloud_id,
    )
    foreign = issue(
        key="SDAX-2",
        main_id="41",
        asset_labels={"41": "Wrong workspace"},
        assets_workspace_id=DEFAULT_TENANT.workspace_id,
        assets_cloud_id=DEFAULT_TENANT.cloud_id,
    )
    foreign["fields"]["customfield_10680"][0]["workspaceId"] = "foreign"
    old = issue(key="SDAX-3")
    old["fields"]["created"] = "2020-01-01T00:00:00Z"
    frame = load_issues_Amparex([normal, foreign, old, issue(key="SDEU-4")])
    save_data(frame, path=data / "jira_data.pkl")
    original = SOURCE.read_bytes()
    notebook = nbformat.reads(original.decode(), as_version=4)
    bootstrap = (
        "import sys\nfrom pathlib import Path\nfrom datetime import date\n"
        f"REPO_ROOT=Path({str(ROOT)!r})\nsys.path.insert(0,str(REPO_ROOT))\n"
        "PROJECT='SDAX'\nSTART_DATE=date(2026,9,1)\nEND_DATE=date(2026,9,3)\n"
        f"RUN_CLASSIFICATION={run!r}\nREFRESH=False\n"
        f"OUTPUT_DIR=Path({str(directory / 'exports')!r})\n"
        + (ROOT / "tests/notebook_bootstrap.py").read_text()
        + "\nblocked_transports = await verify_transports()\n"
    )
    if empty:
        bootstrap += "\nSTART_DATE=date(2030,1,1)\nEND_DATE=date(2030,1,2)\n"
    notebook.cells.insert(0, nbformat.v4.new_code_cell(bootstrap))
    notebook.cells.append(
        nbformat.v4.new_code_cell(
            "state=dict(manifest=manifest, keys=df.key.tolist(), "
            "categories=df.Hauptkategorie.tolist(), "
            "result_keys=results.key.tolist(), labelled_rows=len(labelled), fake_calls=fake_calls, "
            "blocked_transports=blocked_transports, success=int(success.sum()))\n"
            f"Path({str(directory / 'state.json')!r}).write_text(json.dumps(state))"
        )
    )
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("JIRA_", "AZURE_OPENAI_")) and k != "UI_PASSWORD"
    }
    env.update(
        DATA_DIR=str(data),
        IPYTHONDIR=str(directory / "ipython"),
        JUPYTER_CONFIG_DIR=str(directory / "jupyter"),
    )
    manager = KernelManager(kernel_name="python3")
    assert manager.kernel_spec is not None
    manager.kernel_spec.argv = [
        sys.executable,
        "-m",
        "ipykernel_launcher",
        "-f",
        "{connection_file}",
    ]
    client = NotebookClient(
        notebook,
        km=manager,
        timeout=90,
        resources={"metadata": {"path": str(directory)}},
    )
    client.execute(env=env)
    nbformat.write(notebook, directory / "executed.ipynb")
    state = json.loads((directory / "state.json").read_text())
    return dict(
        run=run,
        empty=empty,
        directory=directory,
        state=state,
        before=original,
        after=SOURCE.read_bytes(),
        cells=notebook.cells,
    )


def test_notebook_runs_offline_with_selected_cohort(
    notebook_smoke: dict[str, Any],
) -> None:
    """Both modes select exactly two tickets and block all HTTP implementations in the kernel."""
    result = notebook_smoke["state"]
    expected = [] if notebook_smoke["empty"] else ["SDAX-1", "SDAX-2"]
    assert result["keys"] == expected
    assert result["categories"] == (
        [] if notebook_smoke["empty"] else ["Known category", "Unbekannt"]
    )
    assert result["labelled_rows"] == len(expected) and result["blocked_transports"] == 5
    assert result["manifest"]["selected_rows"] == len(expected)
    assert result["manifest"]["excluded_window_rows"] == (3 if notebook_smoke["empty"] else 1)
    assert result["manifest"]["excluded_project_rows"] == 1
    assert result["success"] == (2 if notebook_smoke["run"] else 0)
    assert all(
        cell.execution_count is not None
        for cell in notebook_smoke["cells"]
        if cell.cell_type == "code"
    )


def test_notebook_smoke_uses_no_real_paths_or_outputs(
    notebook_smoke: dict[str, Any],
) -> None:
    """The tracked notebook remains byte-identical and artifacts match only the temporary source."""
    assert notebook_smoke["before"] == notebook_smoke["after"]
    directory = notebook_smoke["directory"]
    manifest = json.loads((directory / "exports/analysis-manifest.json").read_text())
    digest = hashlib.sha256((directory / "data/jira_data.pkl").read_bytes()).hexdigest()
    assert manifest["extraction_digest"] == digest
    assert (directory / "executed.ipynb").is_file()
    groups = tomllib.loads((ROOT / "pyproject.toml").read_text())["dependency-groups"]
    for name in ("nbclient", "nbformat", "ipykernel"):
        assert any(dep.startswith(name + "==") for dep in groups["analysis"])


def test_no_classification_mode_is_safe_without_credentials(
    notebook_smoke: dict[str, Any],
) -> None:
    """An absent compatible cache is visibly unclassified and constructs no Azure client."""
    result = notebook_smoke["state"]
    expected = ["SDAX-1", "SDAX-2"] if notebook_smoke["run"] else []
    assert result["fake_calls"] == expected and result["result_keys"] == expected
