"""Tracked documentation works from a clean export without local planning files."""

import os
import subprocess
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _git(root: Path, *args: str) -> bytes:
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True).stdout


def test_local_link_checker_covers_paths_anchors_and_fences(tmp_path: Path, capsys) -> None:
    from tools.check_docs import main

    (tmp_path / "über space.md").write_text("# Hello, Welt!\n# Hello, Welt!\n")
    (tmp_path / "image.png").write_bytes(b"fixture")
    readme = tmp_path / "README.md"
    readme.write_text("""# Overview
[unicode](%C3%BCber%20space.md#hello-welt-1)
![image](image.png)
[self](#overview)
[historical source line](README.md#L99999)
[reference][guide]
[guide]: <über space.md#hello-welt>
`[example](missing-inline.md)`
~~~md
[example](missing-fenced.md)
~~~
````md
[example](missing-other.md)
````
[web](https://offline.invalid/absent) [email](mailto:a@example.invalid)
""")
    args = ["--repo-root", str(tmp_path), "--path", str(readme)]
    assert main(args) == 0
    for target, reason in [
        ("absent.md", "missing_file"),
        ("über%20space.md#absent", "missing_anchor"),
        ("../outside.md", "outside_root"),
    ]:
        readme.write_text(f"[bad]({target})\n")
        assert main(args) == 1
        assert reason in capsys.readouterr().out
    (tmp_path / "escape.md").symlink_to(tmp_path.parent / "escape.md")
    readme.write_text("[escape](escape.md)\n")
    assert main(args) == 1
    assert "outside_root" in capsys.readouterr().out


def test_fresh_clone_documentation_has_no_local_only_dependencies(tmp_path: Path) -> None:
    from tools.check_docs import main

    # Export index blobs, never ignored working-copy files; GIT_INDEX_FILE permits
    # validating the intended task index before committing any new documentation.
    for entry in _git(ROOT, "ls-files", "--stage", "-z").split(b"\0"):
        if not entry:
            continue
        meta, name = entry.split(b"\t", 1)
        mode, blob, stage = meta.split()
        assert stage == b"0"
        path = tmp_path / os.fsdecode(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        content = _git(ROOT, "cat-file", "blob", blob.decode())
        if mode == b"120000":
            path.symlink_to(os.fsdecode(content))
        else:
            path.write_bytes(content)
    assert not (tmp_path / "docs/superpowers").exists()
    # Synthetic Git index is independent of the caller's optional alternate index.
    env = {key: value for key, value in os.environ.items() if key != "GIT_INDEX_FILE"}
    for args in [["init", "-q"], ["add", "."]]:
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, env=env)
    with pytest.MonkeyPatch.context() as patch:
        patch.delenv("GIT_INDEX_FILE", raising=False)
        assert main(["--repo-root", str(tmp_path), "--tracked"]) == 0


def test_documented_setup_matches_locked_groups_and_env_contract() -> None:
    guide = (ROOT / "docs/development.md").read_text()
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert {"analysis", "dev"} <= project["dependency-groups"].keys()
    for command in [
        "uv sync --frozen --no-default-groups",
        "uv sync --frozen --group analysis --group dev",
        "uv run --frozen --no-sync --env-file .env streamlit run app.py",
        "python tools/dependency_export.py --check",
        "python cache_maintenance.py migrate",
        "python cache_maintenance.py cleanup",
    ]:
        assert command in guide
    assert "DATA_DIR" in guide and "assets/" in guide and "working directory" in guide
    assert "pytest" in guide and "unittest discover" not in guide
    assert "Anthropic" not in (ROOT / "ticket_classifier.py").read_text().split("import hashlib")[0]
