"""The generated hashed runtime supports its complete entrypoint import closure."""

import json
import os
import subprocess
from pathlib import Path

from tools.runtime_smoke import runtime_modules

ROOT = Path(__file__).resolve().parents[1]


def test_clean_hashed_runtime_install_imports_supported_entrypoints(
    clean_runtime: Path, tmp_path: Path
) -> None:
    modules = runtime_modules(ROOT)
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("JIRA_", "AZURE_OPENAI_", "UI_PASSWORD", "PYTHONPATH"))
    }
    env["PYTHONPATH"] = str(ROOT)
    result = subprocess.run(
        [
            str(clean_runtime),
            str(ROOT / "tools/runtime_smoke.py"),
            "--root",
            str(ROOT),
            "--modules",
            json.dumps(modules),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "runtime-smoke-ok" in result.stdout
