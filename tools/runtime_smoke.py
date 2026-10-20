"""Derive the runtime import closure and exercise it with synthetic data only."""

from __future__ import annotations

import argparse
import ast
import importlib
import importlib.util
import json
import os
import secrets
import shlex
import sys
import tempfile
from collections.abc import Sequence
from contextlib import ExitStack
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

if TYPE_CHECKING:
    from file_io import Revision
    from settings import Settings

ENTRYPOINTS = (
    "app",
    "backfill_jira",
    "backfill_country",
    "asset_migration",
    "jira_country_export",
    "cache_maintenance",
)


def runtime_modules(root: Path) -> list[str]:
    """Derive all local imports, including imports inside functions, from supported entrypoints.

    Args:
        root: Repository source directory.

    Returns:
        Sorted root module names required by the supported runtime.
    """
    pending = list(ENTRYPOINTS)
    found: set[str] = set()
    while pending:
        name = pending.pop()
        if name in found:
            continue
        found.add(name)
        tree = ast.parse((root / (name + ".py")).read_text())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module.split(".")[0]]
            pending.extend(
                candidate
                for candidate in names
                if (root / (candidate + ".py")).is_file() and candidate not in found
            )
    return sorted(found)


def copied_paths(root: Path) -> set[str]:
    """Read explicit COPY sources from the Dockerfile, the sole authored runtime manifest.

    Args:
        root: Repository containing the Dockerfile.

    Returns:
        Source paths copied into the final image.
    """
    paths: set[str] = set()
    for line in (root / "Dockerfile").read_text().replace("\\\n", " ").splitlines():
        tokens = shlex.split(line, comments=True)
        if tokens and tokens[0].upper() == "COPY":
            paths.update(token for token in tokens[1:-1] if not token.startswith("--"))
    return paths


def _require(condition: object, code: str) -> None:
    if not condition:
        raise RuntimeError(code)


def _forbidden(*args: object, **kwargs: object) -> None:
    raise RuntimeError("offline_runtime_network_forbidden")


def _exercise(root: Path) -> None:
    import pandas as pd
    from streamlit.testing.v1 import AppTest

    import data_loading
    import interactive

    # Import the installed widget adapter before replacing only browser rendering.
    importlib.import_module("pygwalker.api.streamlit")
    reads: list[bool] = []
    original = data_loading.load_data_snapshot

    def load(
        path: str | Path | None = None, *, settings: Settings | None = None
    ) -> tuple[pd.DataFrame | None, Revision | None]:
        reads.append(True)
        return original(path=path, settings=settings)

    def run() -> AppTest:
        app = AppTest.from_file(str(root / "app.py"), default_timeout=40).run()
        _require(not app.exception, "app_exception")
        return app

    with (
        patch.object(data_loading, "load_data_snapshot", load),
        patch.object(interactive, "render_interactive"),
    ):
        os.environ.pop("UI_PASSWORD", None)
        app = run()
        _require(
            reads and any("Keine Jira-Daten" in item.value for item in app.info),
            "absent_cache_state",
        )
        first_password = secrets.token_urlsafe(24)
        os.environ["UI_PASSWORD"] = first_password
        reads.clear()
        app = run()
        _require(not reads and len(app.text_input) == 1, "password_before_cache")
        app.session_state["_ui_authed"] = True
        app.run()
        _require(not reads and len(app.text_input) == 1, "legacy_session_rejected")
        app.text_input[0].set_value(first_password).run()
        _require(reads and not app.text_input, "authenticated_read")
        reads.clear()
        os.environ["UI_PASSWORD"] = secrets.token_urlsafe(24)
        app.run()
        _require(not reads and len(app.text_input) == 1, "rotation_revokes_before_read")
        os.environ.pop("UI_PASSWORD", None)
        tickets = Path(os.environ["DATA_DIR"]) / "jira_data.pkl"
        frame = pd.DataFrame(
            [
                {
                    "key": "SDAX-1",
                    "created": pd.Timestamp.now(tz="Europe/Berlin"),
                    "currentstatus_name": "Offen",
                    "currentstatus_date": None,
                    "status_category": "new",
                    "project_key": "SDAX",
                    "firma": "Amparex",
                }
            ]
        )
        frame.to_pickle(tickets)
        before = tickets.read_bytes()
        app = run()
        _require(len(app.tabs) == 13 and tickets.read_bytes() == before, "legacy_cache_render")
        tickets.write_bytes(b"synthetic-corrupt-cache")
        app = run()
        _require(
            any("Cache konnte nicht geladen" in item.value for item in app.error),
            "corrupt_cache_error",
        )
    print(
        "runtime-smoke-ok: imports, public, gate, legacy session, rotation, absent, legacy, corrupt"
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the runtime smoke in temporary storage, with all available HTTP transports blocked.

    Args:
        argv: Optional root/module inventory arguments; inventory comes from source AST.

    Returns:
        Zero when every synthetic runtime contract succeeds.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--modules", required=True)
    args = parser.parse_args(argv)
    modules = json.loads(args.modules)
    _require(
        isinstance(modules, list)
        and all(isinstance(name, str) and name.isidentifier() for name in modules),
        "invalid_module_inventory",
    )
    root = args.root.resolve()
    # A fresh temporary directory is always selected; caller data is never loaded.
    parent = os.environ.get("DATA_DIR")
    if parent and not Path(parent).is_dir():
        Path(parent).mkdir(parents=True)
    original_main = sys.modules.get("__main__")
    with (
        tempfile.TemporaryDirectory(prefix="evex-runtime-", dir=parent) as directory,
        ExitStack() as stack,
    ):
        stack.enter_context(patch.dict(os.environ, {"DATA_DIR": directory}))
        requests = importlib.import_module("requests")
        stack.enter_context(patch.object(requests.sessions.Session, "request", _forbidden))
        for name in ("httpx", "httpx2"):
            if importlib.util.find_spec(name):
                client = importlib.import_module(name)
                stack.enter_context(
                    patch.object(client.HTTPTransport, "handle_request", _forbidden)
                )
                stack.enter_context(
                    patch.object(client.AsyncHTTPTransport, "handle_async_request", _forbidden)
                )
        for name in modules:
            if name != "app":
                importlib.import_module(name)
        for name in ("openai", "pptx", "pytest"):
            _require(
                importlib.util.find_spec(name) is None, "unexpected_analysis_or_dev_dependency"
            )
        try:
            _exercise(root)
        finally:
            if original_main is not None:
                sys.modules["__main__"] = original_main
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
