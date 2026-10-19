"""Shared setup that keeps AppTest cases independent of a developer's UI_PASSWORD."""

import os
from unittest.mock import patch

# These load .env into the environment on first import. Import them now so a lazy
# import during a test cannot restore UI_PASSWORD after it was removed.
import data_transformation  # noqa: F401
import jira_loader  # noqa: F401


def without_ui_password(test):
    """Run a test without UI_PASSWORD from the shell or .env; tests may set it."""
    test.enterContext(patch.dict(os.environ))
    os.environ.pop("UI_PASSWORD", None)
    test.enterContext(patch("ui_auth.ENV_FILE", os.devnull))
