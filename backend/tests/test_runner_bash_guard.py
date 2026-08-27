import sys

import pytest

from app import paths
from app.general_skills import runner


def test_bash_unsupported_on_windows(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    assert runner._bash_supported() is False


def test_bash_unsupported_when_frozen(monkeypatch) -> None:
    monkeypatch.setattr(paths, "is_frozen", lambda: True)
    assert runner._bash_supported() is False


@pytest.mark.skipif(sys.platform == "win32", reason="bash runtime is intentionally unsupported on Windows")
def test_bash_supported_on_dev_posix(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(paths, "is_frozen", lambda: False)
    assert runner._bash_supported() is True
