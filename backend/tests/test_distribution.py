"""Verify the trusted repository identity embedded in StaffDeck packages."""

from __future__ import annotations

import importlib
import json
import subprocess
from pathlib import Path
from types import ModuleType

import pytest


def _distribution_module() -> ModuleType:
    """Load the production boundary and turn a missing module into explicit RED evidence."""
    try:
        return importlib.import_module("app.distribution")
    except ModuleNotFoundError:
        pytest.fail("app.distribution is not implemented yet")


def _git(repository: Path, *arguments: str) -> None:
    """Create controlled local Git state for origin-resolution tests without network access."""
    subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )


@pytest.mark.parametrize(
    "repository",
    ["OpenBMB/StaffDeck", "JnyRoad/StaffDeck", "owner-1/repo_name", "a/repo.with-dots"],
)
def test_validate_release_repository_accepts_canonical_identity(repository: str) -> None:
    """Catch rejection or rewriting of valid GitHub owner/repository identities."""
    distribution = _distribution_module()

    assert distribution.validate_release_repository(repository) == repository


@pytest.mark.parametrize(
    "repository",
    [
        None,
        "",
        " OpenBMB/StaffDeck",
        "OpenBMB/StaffDeck ",
        "OpenBMB",
        "OpenBMB/StaffDeck/extra",
        "-OpenBMB/StaffDeck",
        "OpenBMB-/StaffDeck",
        "OpenBMB/.",
        "OpenBMB/..",
        "OpenBMB/StaffDeck.git",
        "OpenBMB/staff deck",
        "https://github.com/OpenBMB/StaffDeck",
        "git@github.com:OpenBMB/StaffDeck.git",
        "OpenBMB/StaffDeck?tab=releases",
        "OpenBMB/StaffDeck#latest",
    ],
)
def test_validate_release_repository_rejects_noncanonical_identity(
    repository: str | None,
) -> None:
    """Catch acceptance of URL syntax, traversal segments, or malformed repository names."""
    distribution = _distribution_module()

    assert distribution.validate_release_repository(repository) is None


def test_frozen_release_repository_uses_bundled_metadata_and_ignores_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Catch runtime environment overrides changing an installed package's trusted distributor."""
    distribution = _distribution_module()
    metadata = tmp_path / "staffdeck-distribution.json"
    metadata.write_text(
        json.dumps({"release_repository": "JnyRoad/StaffDeck"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(distribution.sys, "frozen", True, raising=False)
    monkeypatch.setattr(distribution.sys, "_MEIPASS", str(tmp_path), raising=False)
    monkeypatch.setenv("STAFFDECK_RELEASE_REPOSITORY", "SomeoneElse/StaffDeck")

    assert distribution.release_repository() == "JnyRoad/StaffDeck"


@pytest.mark.parametrize(
    "content",
    ["", "not-json", "{}", '{"release_repository":"https://github.com/OpenBMB/StaffDeck"}'],
)
def test_frozen_release_repository_fails_closed_for_invalid_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    content: str,
) -> None:
    """Catch malformed package metadata silently falling back to an unrelated repository."""
    distribution = _distribution_module()
    (tmp_path / "staffdeck-distribution.json").write_text(content, encoding="utf-8")
    monkeypatch.setattr(distribution.sys, "frozen", True, raising=False)
    monkeypatch.setattr(distribution.sys, "_MEIPASS", str(tmp_path), raising=False)
    monkeypatch.setenv("STAFFDECK_RELEASE_REPOSITORY", "SomeoneElse/StaffDeck")

    assert distribution.release_repository() is None


def test_frozen_release_repository_fails_closed_when_metadata_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Catch a missing bundled identity being replaced by a runtime environment value."""
    distribution = _distribution_module()
    monkeypatch.setattr(distribution.sys, "frozen", True, raising=False)
    monkeypatch.setattr(distribution.sys, "_MEIPASS", str(tmp_path), raising=False)
    monkeypatch.setenv("STAFFDECK_RELEASE_REPOSITORY", "SomeoneElse/StaffDeck")

    assert distribution.release_repository() is None


def test_write_distribution_metadata_emits_exact_valid_payload(tmp_path: Path) -> None:
    """Catch packaging metadata that is invalid, incomplete, or written to the wrong path."""
    distribution = _distribution_module()
    destination = tmp_path / "build" / "staffdeck-distribution.json"

    written_path = distribution.write_distribution_metadata(
        destination,
        "JnyRoad/StaffDeck",
    )

    assert written_path == destination
    assert json.loads(destination.read_text(encoding="utf-8")) == {
        "release_repository": "JnyRoad/StaffDeck"
    }


def test_build_repository_prefers_explicit_identity_over_github_actions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Catch an explicit local build identity being ignored in favor of ambient CI state."""
    distribution = _distribution_module()
    monkeypatch.setenv("STAFFDECK_RELEASE_REPOSITORY", "JnyRoad/StaffDeck")
    monkeypatch.setenv("GITHUB_REPOSITORY", "OpenBMB/StaffDeck")

    assert distribution.resolve_build_release_repository(tmp_path) == "JnyRoad/StaffDeck"


def test_build_repository_uses_github_actions_repository(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Catch release workflows failing to bind packages to the repository running the build."""
    distribution = _distribution_module()
    monkeypatch.delenv("STAFFDECK_RELEASE_REPOSITORY", raising=False)
    monkeypatch.setenv("GITHUB_REPOSITORY", "JnyRoad/StaffDeck")

    assert distribution.resolve_build_release_repository(tmp_path) == "JnyRoad/StaffDeck"


@pytest.mark.parametrize(
    "remote_url",
    [
        "https://github.com/JnyRoad/StaffDeck.git",
        "git@github.com:JnyRoad/StaffDeck.git",
        "ssh://git@github.com/JnyRoad/StaffDeck.git",
    ],
)
def test_build_repository_derives_valid_github_origin(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    remote_url: str,
) -> None:
    """Catch supported GitHub remote forms resolving to different package identities."""
    distribution = _distribution_module()
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "remote", "add", "origin", remote_url)
    monkeypatch.delenv("STAFFDECK_RELEASE_REPOSITORY", raising=False)
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)

    assert distribution.resolve_build_release_repository(tmp_path) == "JnyRoad/StaffDeck"


@pytest.mark.parametrize("remote_url", [None, "https://gitlab.com/JnyRoad/StaffDeck.git"])
def test_build_repository_uses_safe_default_without_github_origin(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    remote_url: str | None,
) -> None:
    """Catch source archives or foreign remotes producing an undefined update identity."""
    distribution = _distribution_module()
    _git(tmp_path, "init", "-q")
    if remote_url is not None:
        _git(tmp_path, "remote", "add", "origin", remote_url)
    monkeypatch.delenv("STAFFDECK_RELEASE_REPOSITORY", raising=False)
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)

    assert distribution.resolve_build_release_repository(tmp_path) == "OpenBMB/StaffDeck"


def test_build_repository_rejects_invalid_explicit_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Catch malformed explicit build input silently falling through to another distributor."""
    distribution = _distribution_module()
    monkeypatch.setenv(
        "STAFFDECK_RELEASE_REPOSITORY",
        "https://github.com/JnyRoad/StaffDeck",
    )
    monkeypatch.setenv("GITHUB_REPOSITORY", "OpenBMB/StaffDeck")

    with pytest.raises(ValueError, match="STAFFDECK_RELEASE_REPOSITORY"):
        distribution.resolve_build_release_repository(tmp_path)


def test_source_release_repository_uses_valid_development_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch non-frozen test and development processes ignoring an explicit valid identity."""
    distribution = _distribution_module()
    monkeypatch.setattr(distribution.sys, "frozen", False, raising=False)
    monkeypatch.setenv("STAFFDECK_RELEASE_REPOSITORY", "JnyRoad/StaffDeck")

    assert distribution.release_repository() == "JnyRoad/StaffDeck"


def test_source_release_repository_defaults_to_upstream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch source update opt-in losing its existing upstream repository default."""
    distribution = _distribution_module()
    monkeypatch.setattr(distribution.sys, "frozen", False, raising=False)
    monkeypatch.delenv("STAFFDECK_RELEASE_REPOSITORY", raising=False)

    assert distribution.release_repository() == "OpenBMB/StaffDeck"


def test_source_release_repository_rejects_invalid_development_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch malformed source overrides silently falling back to an unrelated repository."""
    distribution = _distribution_module()
    monkeypatch.setattr(distribution.sys, "frozen", False, raising=False)
    monkeypatch.setenv("STAFFDECK_RELEASE_REPOSITORY", "OpenBMB/StaffDeck/extra")

    assert distribution.release_repository() is None
