"""Provide filesystem isolation shared by backend tests."""

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolate_user_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Route per-test user-home storage to pytest's writable temporary directory.

    The fixture changes only process environment for one test, so production
    resolution of ``~/.staffdeck/workspaces`` remains unchanged.
    """
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
