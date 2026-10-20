"""Only an explicitly selected local image build touches the Docker daemon."""

import json
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest
from test_container_contract import _disabled_protections

from tools.runtime_smoke import copied_paths, runtime_modules

ROOT = Path(__file__).resolve().parents[1]


def _docker(
    *args: str, input: str | None = None, check: bool = True
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", *args], input=input, text=True, capture_output=True, check=check, timeout=900
    )


@dataclass
class RuntimeImage:
    name: str
    config: dict
    files: set[str]


@pytest.fixture(scope="module")
def runtime_image(request: pytest.FixtureRequest) -> Iterator[RuntimeImage]:
    if not request.config.getoption("run_runtime_image", default=False):
        pytest.skip("requires --run-runtime-image")
    name = "evex-cleanup-test-" + uuid4().hex
    try:
        built = _docker("build", "--tag", name, str(ROOT), check=False)
        assert built.returncode == 0, built.stdout + built.stderr
        config = json.loads(_docker("image", "inspect", name).stdout)[0]["Config"]
        result = _docker(
            "run",
            "--rm",
            "--network=none",
            name,
            "python",
            "-c",
            "import os,json;print(json.dumps([os.path.join(r,f).removeprefix('/') "
            "for r,ds,fs in os.walk('/app') for f in fs]))",
        )
        yield RuntimeImage(name, config, set(json.loads(result.stdout)))
    finally:
        _docker("image", "rm", name, check=False)


def test_runtime_allowlist_equals_transitive_import_closure() -> None:
    modules = runtime_modules(ROOT)
    expected = {module + ".py" for module in modules}
    paths = copied_paths(ROOT)
    assert {path for path in paths if path.endswith(".py")} == expected
    assert {
        "requirements.runtime.txt",
        ".streamlit/config.toml",
        "assets/evex_logo.png",
        "assets/object_id_to_name.json",
    } <= paths
    assert not any("*" in path or path in {".", "./"} for path in paths)
    assert len(modules) >= 30


def test_runtime_image_contents_match_supported_import_closure(runtime_image: RuntimeImage) -> None:
    expected = {"app/" + path for path in copied_paths(ROOT)}
    assert runtime_image.files == expected
    assert runtime_image.config["User"] == "10001"
    assert runtime_image.config["Healthcheck"]["Test"]
    assert not any(
        p.startswith(("app/tests/", "app/tools/", "app/infra/", "app/analysis/"))
        or p.endswith((".ipynb", "/.env"))
        for p in runtime_image.files
    )
    writable = _docker(
        "run",
        "--rm",
        "--network=none",
        runtime_image.name,
        "python",
        "-c",
        "import os,pathlib;assert os.getuid()==10001;"
        "pathlib.Path('/app/data/probe').write_text('synthetic')",
    )
    assert writable.returncode == 0


def test_container_health_password_rotation_and_cache_states(runtime_image: RuntimeImage) -> None:
    container = "evex-cleanup-health-" + uuid4().hex
    try:
        _docker(
            "run",
            "-d",
            "--name",
            container,
            "--network=none",
            "--tmpfs",
            "/app/data:rw,uid=10001,gid=10001",
            "-e",
            "UI_PASSWORD=synthetic-test-password",
            "-e",
            "PORT=8513",
            runtime_image.name,
        )
        health = "import urllib.request;assert urllib.request.urlopen('http://127.0.0.1:8513/_stcore/health',timeout=2).status==200"
        for _ in range(40):
            result = _docker("exec", container, "python", "-c", health, check=False)
            if result.returncode == 0:
                break
            time.sleep(0.25)
        assert result.returncode == 0, _docker("logs", container).stdout
        result = _docker(
            "exec",
            "-i",
            container,
            "python",
            "-",
            "--modules",
            json.dumps(runtime_modules(ROOT)),
            input=(ROOT / "tools/runtime_smoke.py").read_text(),
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "runtime-smoke-ok" in result.stdout
        assert (
            "/app/data"
            in json.loads(_docker("inspect", container).stdout)[0]["HostConfig"]["Tmpfs"]
        )
        mounted = _docker(
            "exec",
            container,
            "python",
            "-c",
            "from pathlib import Path;print(next(line.split()[2] "
            "for line in Path('/proc/mounts').read_text().splitlines() "
            "if line.split()[1]=='/app/data'))",
        )
        assert mounted.stdout.strip() == "tmpfs"
    finally:
        _docker("rm", "-f", container, check=False)


def test_image_command_retains_cross_site_protections(runtime_image: RuntimeImage) -> None:
    assert (
        _disabled_protections(
            (ROOT / "Dockerfile").read_text(), (ROOT / ".streamlit/config.toml").read_text()
        )
        == set()
    )
    assert runtime_image.config["Cmd"] == json.loads(
        next(
            line[4:]
            for line in (ROOT / "Dockerfile").read_text().splitlines()
            if line.startswith("CMD ")
        )
    )
    assert not any(
        "ENABLE_CORS=false" in item or "ENABLE_XSRF_PROTECTION=false" in item
        for item in runtime_image.config["Env"]
    )
