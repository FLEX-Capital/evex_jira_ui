"""Immutable environment configuration with lazy operation credentials."""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

DEFAULT_CLOUD_ID = "242cf880-c51a-4277-9381-781d5ae181df"
DEFAULT_WORKSPACE_ID = "9926cb30-3f07-4fb2-9c83-aa4fc551c721"
TENANT_ATTR = "evex_jira_tenant"
DEFAULT_API_VERSION = "2024-10-21"
PROJECT_ROOT = Path(__file__).resolve().parent


class SettingsError(RuntimeError):
    """A configuration error containing safe codes and variable names only."""

    def __init__(self, code: str, names: tuple[str, ...]) -> None:
        self.code = code
        self.names = names
        super().__init__(f"{code}: {', '.join(names)}")


@dataclass(frozen=True)
class TenantIdentity:
    """Identity of the Jira cloud and Assets workspace."""

    cloud_id: str
    workspace_id: str

    def as_dict(self) -> dict[str, str]:
        """Return the serializable full tenant identity for owned artifacts."""
        return {"cloud_id": self.cloud_id, "workspace_id": self.workspace_id}


DEFAULT_TENANT = TenantIdentity(DEFAULT_CLOUD_ID, DEFAULT_WORKSPACE_ID)


@dataclass(frozen=True)
class JiraConnection:
    """Validated credentials captured for one requested Jira operation."""

    url: str
    username: str = field(repr=False)
    password: str = field(repr=False)


@dataclass(frozen=True)
class ModelConnection:
    """Validated deployment credentials captured for classification."""

    endpoint: str
    api_key: str = field(repr=False)
    deployment: str
    api_version: str


@dataclass(frozen=True)
class DataPaths:
    """Absolute mutable paths and project-owned static assets."""

    root: Path
    tickets: Path
    countries: Path
    classifications: Path
    raw_snapshot: Path
    raw_metadata: Path
    import_status: Path
    assets: Path
    logo: Path
    category_map: Path

    def checkpoint(self, project: str) -> Path:
        """Resolve a known desk's checkpoint under this captured data root."""
        from service_desks import BY_KEY

        if project not in BY_KEY:
            raise SettingsError("invalid_project", ("project",))
        return self.root / f"backfill_{project}.json"


@dataclass(frozen=True)
class Settings:
    """An offline snapshot; network credentials are validated only on demand."""

    paths: DataPaths
    tenant: TenantIdentity
    ui_password: str | None = field(repr=False)
    jira_url: str | None
    jira_username: str | None = field(repr=False)
    jira_password: str | None = field(repr=False)
    azure_endpoint: str | None
    azure_api_key: str | None = field(repr=False)
    azure_deployment: str | None
    azure_api_version: str

    def require_jira(self) -> JiraConnection:
        """Require Jira credentials for an explicitly requested operation."""
        missing = tuple(
            name
            for name, value in (
                ("JIRA_URL", self.jira_url),
                ("JIRA_USERNAME", self.jira_username),
                ("JIRA_PASSWORD", self.jira_password),
            )
            if not value
        )
        if not self.jira_url or not self.jira_username or not self.jira_password:
            raise SettingsError("missing_jira", missing)
        return JiraConnection(self.jira_url, self.jira_username, self.jira_password)

    def require_classifier(self, deployment: str | None = None) -> ModelConnection:
        """Require model credentials, accepting an explicit deployment override."""
        selected = _clean(deployment) or self.azure_deployment
        missing = tuple(
            name
            for name, value in (
                ("AZURE_OPENAI_ENDPOINT", self.azure_endpoint),
                ("AZURE_OPENAI_API_KEY", self.azure_api_key),
                ("AZURE_OPENAI_DEPLOYMENT_NAME", selected),
            )
            if not value
        )
        if not self.azure_endpoint or not self.azure_api_key or not selected:
            raise SettingsError("missing_classifier", missing)
        return ModelConnection(
            self.azure_endpoint, self.azure_api_key, selected, self.azure_api_version
        )


def _clean(value: str | None) -> str | None:
    return value.strip() or None if value is not None else None


def _url(value: str | None, name: str) -> str | None:
    value = _clean(value)
    if value is None:
        return None
    try:
        parts = urlsplit(value)
        valid = (
            parts.scheme in {"http", "https"}
            and bool(parts.hostname)
            and parts.username is None
            and parts.password is None
            and not any(char.isspace() for char in value)
        )
        _ = parts.port
    except ValueError:
        valid = False
    if not valid:
        raise SettingsError("invalid_url", (name,))
    return value.rstrip("/")


def _identity(value: str | None, default: str, name: str) -> str:
    try:
        return str(UUID(_clean(value) or default))
    except ValueError:
        raise SettingsError("invalid_identity", (name,)) from None


def load_settings(
    environ: Mapping[str, str] | None = None, *, project_root: Path | None = None
) -> Settings:
    """Read environment values without reading files, creating paths or networking.

    Args:
        environ: Explicit snapshot, or the process environment when omitted.
        project_root: Base for relative DATA_DIR and static assets.

    Returns:
        Immutable values with secrets omitted from representations.

    Raises:
        SettingsError: Invalid identities or service URLs, with safe names only.
    """
    env = os.environ if environ is None else environ
    project = (project_root or PROJECT_ROOT).resolve()
    configured_root = Path(_clean(env.get("DATA_DIR")) or "data")
    root = (
        configured_root if configured_root.is_absolute() else project / configured_root
    ).resolve()
    assets = project / "assets"
    paths = DataPaths(
        root,
        root / "jira_data.pkl",
        root / "asset_country.json",
        root / "sdax_classifications.parquet",
        root / "jira_issues.json",
        root / "jira_issues.meta.json",
        root / "import_status.json",
        assets,
        assets / "evex_logo.png",
        assets / "object_id_to_name.json",
    )
    return Settings(
        paths=paths,
        tenant=TenantIdentity(
            _identity(env.get("JIRA_CLOUD_ID"), DEFAULT_CLOUD_ID, "JIRA_CLOUD_ID"),
            _identity(
                env.get("JIRA_WORKSPACE_ID"), DEFAULT_WORKSPACE_ID, "JIRA_WORKSPACE_ID"
            ),
        ),
        ui_password=env.get("UI_PASSWORD") or None,
        jira_url=_url(env.get("JIRA_URL"), "JIRA_URL"),
        jira_username=_clean(env.get("JIRA_USERNAME")),
        jira_password=env.get("JIRA_PASSWORD") or None,
        azure_endpoint=_url(env.get("AZURE_OPENAI_ENDPOINT"), "AZURE_OPENAI_ENDPOINT"),
        azure_api_key=env.get("AZURE_OPENAI_API_KEY") or None,
        azure_deployment=_clean(env.get("AZURE_OPENAI_DEPLOYMENT_NAME"))
        or _clean(env.get("AZURE_OPENAI_DEPLOYMENT")),
        azure_api_version=_clean(env.get("AZURE_OPENAI_API_VERSION"))
        or DEFAULT_API_VERSION,
    )
