"""Offline guards cover both HTTPX implementations and preserve fake transports."""

import asyncio
import fnmatch
import importlib
import socket
import tomllib
from pathlib import Path

import pytest
import requests
import yaml

from tools.runtime_smoke import runtime_modules

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def no_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError("unexpected DNS lookup")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)


def test_requests_and_httpx_sync_async_are_blocked(no_dns: None) -> None:
    with pytest.raises(AssertionError, match="offline"):
        requests.get("https://offline-test.invalid")
    for name in ("httpx", "httpx2"):
        http = importlib.import_module(name)
        with http.Client(trust_env=False) as client:
            with pytest.raises(AssertionError, match="offline"):
                client.get("https://offline-test.invalid")

        async def probe():
            async with http.AsyncClient(trust_env=False) as client:
                with pytest.raises(AssertionError, match="offline"):
                    await client.get("https://offline-test.invalid")

        asyncio.run(probe())


def test_both_httpx_mock_transports_remain_usable(no_dns: None) -> None:
    for name in ("httpx", "httpx2"):
        http = importlib.import_module(name)

        def response(request):
            return http.Response(200, json={"synthetic": True})

        with http.Client(transport=http.MockTransport(response)) as client:
            assert client.get("https://offline-test.invalid").json() == {"synthetic": True}

        async def probe():
            async with http.AsyncClient(transport=http.MockTransport(response)) as client:
                assert (await client.get("https://offline-test.invalid")).json() == {
                    "synthetic": True
                }

        asyncio.run(probe())


def test_real_sdk_cannot_escape_through_default_transport(no_dns: None) -> None:
    from openai import APIConnectionError, AzureOpenAI

    with AzureOpenAI(
        azure_endpoint="https://offline-test.invalid",
        api_key="synthetic",
        api_version="2024-10-21",
        max_retries=0,
    ) as client:
        with pytest.raises(APIConnectionError) as caught:
            client.chat.completions.create(
                model="synthetic", messages=[{"role": "user", "content": "synthetic"}]
            )
    assert isinstance(caught.value.__cause__, AssertionError)
    assert "offline" in str(caught.value.__cause__)


def test_ci_contract_executes_all_required_gates() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    jobs = workflow["jobs"]
    commands = "\n".join(step.get("run", "") for job in jobs.values() for step in job["steps"])
    for required in [
        "uv sync --frozen --group analysis --group dev",
        "python tools/dependency_export.py --check",
        "python tools/check_python.py --all",
        "uv run --frozen --no-sync mypy",
        "python tools/notebook_outputs.py --check",
        "python tools/check_docs.py --tracked",
        "--cov-fail-under=80",
        "--run-runtime-install",
        "--run-runtime-image",
        "--cov=analysis_workflow",
        "--cov=build_board_deck",
    ]:
        assert required in commands
    assert {"test", "lint", "runtime-install", "runtime-image"} <= jobs.keys()
    assert "--filter=-mypy" not in commands
    assert workflow["permissions"] == {"contents": "read"}
    comment = next(step for step in jobs["test"]["steps"] if step.get("name") == "Coverage comment")
    assert "head.repo.full_name == github.repository" in comment["if"]
    assert jobs["test"]["permissions"]["pull-requests"] == "write"
    for job in jobs.values():
        assert any("3.12" in step.get("run", "") for step in job["steps"])


def test_application_coverage_scope_excludes_only_declared_analysis() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["coverage"]["run"]
    assert config["source"] == ["."]
    assert set(config["omit"]) == {
        "tests/*",
        "tools/*",
        "analysis/*",
        "*.ipynb",
        "analysis_workflow.py",
        "ticket_classifier.py",
        "classification_cache.py",
        "build_board_deck.py",
    }
    for module in runtime_modules(ROOT):
        assert not any(fnmatch.fnmatch(module + ".py", pattern) for pattern in config["omit"])


def test_separate_analysis_coverage_cannot_merge_into_application(tmp_path: Path) -> None:
    import coverage

    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    step = next(
        step
        for step in workflow["jobs"]["test"]["steps"]
        if step.get("name") == "Separate analysis and historical deck coverage"
    )
    separate = tmp_path / step["env"]["COVERAGE_FILE"]
    app_path = str(tmp_path / "app.py")
    analysis_path = str(tmp_path / "analysis_workflow.py")
    for path, source in [(tmp_path / ".coverage.synthetic", app_path), (separate, analysis_path)]:
        data = coverage.CoverageData(basename=str(path))
        data.add_lines({source: {1}})
        data.write()
    combined = coverage.Coverage(data_file=str(tmp_path / ".coverage"), config_file=False)
    combined.combine(data_paths=[str(tmp_path)], strict=True)
    assert combined.get_data().measured_files() == {app_path}
    assert separate.exists()
