"""Environment snapshots stay offline, lazy, deterministic and safe to display."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_load_settings_never_reads_dotenv_or_requires_secrets(tmp_path: Path) -> None:
    """Loading an offline snapshot never reads adjacent environment files."""
    from settings import SettingsError, load_settings

    (tmp_path / ".env").write_text("UI_PASSWORD=SYNTHETIC_PRIVATE_MARKER\n")
    with patch.object(Path, "read_text", side_effect=AssertionError("unexpected read")):
        config = load_settings({}, project_root=tmp_path)
    assert config.ui_password is None
    with pytest.raises(SettingsError) as caught:
        config.require_jira()
    assert set(caught.value.names) == {"JIRA_URL", "JIRA_USERNAME", "JIRA_PASSWORD"}
    with pytest.raises(SettingsError) as caught:
        config.require_classifier()
    assert set(caught.value.names) == {
        "AZURE_OPENAI_ENDPOINT",
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_DEPLOYMENT_NAME",
    }


def test_configuration_change_uses_a_new_client_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Independent operations use current credentials instead of a cached client."""
    import data_transformation as transform
    import jira_loader
    from settings import load_settings

    monkeypatch.setenv("JIRA_URL", "https://jira.example")
    monkeypatch.setenv("JIRA_USERNAME", "test-user")
    if hasattr(transform.get_jira, "cache_clear"):
        transform.get_jira.cache_clear()
    with patch.object(transform, "JIRA", side_effect=[object(), object()]) as constructor:
        monkeypatch.setenv("JIRA_PASSWORD", "SYNTHETIC_A")
        first = transform.get_jira()
        monkeypatch.setenv("JIRA_PASSWORD", "SYNTHETIC_B")
        second = transform.get_jira()
    assert first is not second and constructor.call_count == 2
    assert constructor.call_args_list[0].kwargs["basic_auth"] == (
        "test-user",
        "SYNTHETIC_A",
    )
    assert constructor.call_args_list[1].kwargs["basic_auth"] == (
        "test-user",
        "SYNTHETIC_B",
    )
    client = Mock()
    client.myself.return_value = {"timeZone": "UTC"}
    client.enhanced_search_issues.return_value = {"issues": []}
    with patch.object(jira_loader, "JIRA", return_value=client) as constructor:
        for token in ("SYNTHETIC_A", "SYNTHETIC_B"):
            monkeypatch.setenv("JIRA_PASSWORD", token)
            jira_loader.fetch_jira_issues(project="SDAX", save_path=None)
    assert [c.kwargs["basic_auth"][1] for c in constructor.call_args_list] == [
        "SYNTHETIC_A",
        "SYNTHETIC_B",
    ]
    config = load_settings()
    assert "SYNTHETIC_B" not in repr(config)
    assert "SYNTHETIC_B" not in repr(config.require_jira())


def test_environment_launcher_preserves_password_generation(tmp_path: Path) -> None:
    """Plain Python ignores .env while uv explicitly injects the same file."""
    stub = tmp_path / "app"
    stub.mkdir()
    shutil.copyfile(ROOT / "ui_auth.py", stub / "ui_auth.py")
    shutil.copyfile(ROOT / "settings.py", stub / "settings.py")
    env_file = stub / ".env"
    env_file.write_text("UI_PASSWORD=SYNTHETIC_LAUNCH_PASSWORD\n")
    environ = dict(os.environ, UV_CACHE_DIR=str(tmp_path / "uv-cache"))
    environ.pop("UI_PASSWORD", None)
    script = "import json,ui_auth; print(json.dumps({'gated':bool(ui_auth.configured_password())}))"
    plain = subprocess.run(
        [sys.executable, "-c", script],
        cwd=stub,
        env=environ,
        capture_output=True,
        text=True,
        check=True,
    )
    injected = subprocess.run(
        [
            "uv",
            "run",
            "--no-project",
            "--no-sync",
            "--env-file",
            str(env_file),
            sys.executable,
            "-c",
            script,
        ],
        cwd=stub,
        env=environ,
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(plain.stdout) == {"gated": False}
    assert json.loads(injected.stdout) == {"gated": True}


@pytest.mark.parametrize(
    "name,value",
    [
        ("JIRA_CLOUD_ID", "SYNTHETIC_PRIVATE_MARKER"),
        ("JIRA_WORKSPACE_ID", "not-a-uuid"),
        ("JIRA_URL", "ftp://example.test"),
        ("JIRA_URL", "https://user:SYNTHETIC_PRIVATE_MARKER@example.test"),
        ("JIRA_URL", "https://"),
        ("AZURE_OPENAI_ENDPOINT", "file:///SYNTHETIC_PRIVATE_MARKER"),
    ],
)
def test_environment_values_validate_without_eager_network(name: str, value: str) -> None:
    """Invalid environment metadata fails with variable names, never its value."""
    from settings import SettingsError, load_settings

    with pytest.raises(SettingsError) as caught:
        load_settings({name: value})
    assert caught.value.names == (name,)
    assert "SYNTHETIC_PRIVATE_MARKER" not in str(caught.value)
    assert "SYNTHETIC_PRIVATE_MARKER" not in repr(caught.value)


def test_aliases_whitespace_and_secret_representations() -> None:
    """Nonsecret values trim, secret whitespace survives, and aliases are explicit."""
    from settings import load_settings

    config = load_settings(
        {
            "UI_PASSWORD": " päss ",
            "JIRA_URL": " https://jira.example/ ",
            "JIRA_USERNAME": " test-user ",
            "JIRA_PASSWORD": "SYNTHETIC_PRIVATE_MARKER",
            "AZURE_OPENAI_ENDPOINT": "http://localhost:1234",
            "AZURE_OPENAI_API_KEY": "SYNTHETIC_PRIVATE_MARKER",
            "AZURE_OPENAI_DEPLOYMENT_NAME": " primary ",
            "AZURE_OPENAI_DEPLOYMENT": "fallback",
        }
    )
    assert config.ui_password == " päss "
    assert config.require_jira().username == "test-user"
    assert config.azure_deployment == "primary"
    assert config.require_classifier().api_version == "2024-10-21"
    assert config.require_classifier("override").deployment == "override"
    assert "SYNTHETIC_PRIVATE_MARKER" not in repr(config)
    assert "SYNTHETIC_PRIVATE_MARKER" not in repr(config.require_classifier())
    assert load_settings({"UI_PASSWORD": ""}).ui_password is None
    assert load_settings({"AZURE_OPENAI_DEPLOYMENT": "fallback"}).azure_deployment == "fallback"
