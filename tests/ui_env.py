"""Shared setup that keeps AppTest cases independent of a developer's UI_PASSWORD."""

import os
from unittest import TestCase
from unittest.mock import patch


def without_ui_password(test: TestCase) -> None:
    """Run a test without UI_PASSWORD from the process; tests may set it."""
    test.enterContext(patch.dict(os.environ))
    os.environ.pop("UI_PASSWORD", None)
