from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from app import version
from app.api import app_updates


def _feed(*entries: tuple[str, str], repository: str = "OpenBMB/StaffDeck") -> bytes:
    """Build a literal GitHub Atom feed for one repository and the supplied release entries."""
    body = "".join(
        (
            "<entry>"
            f"<updated>{published_at}</updated>"
            f'<link rel="alternate" href="https://github.com/{repository}/releases/tag/{tag}" />'
            f"<title>{tag}</title>"
            "</entry>"
        )
        for tag, published_at in entries
    )
    return f'<feed xmlns="http://www.w3.org/2005/Atom">{body}</feed>'.encode()


def _response(content: bytes) -> httpx.Response:
    """Wrap feed bytes in the complete HTTP response shape consumed by the update checker."""
    return httpx.Response(
        200,
        content=content,
        request=httpx.Request("GET", "https://github.com/OpenBMB/StaffDeck/releases.atom"),
    )


@pytest.fixture(autouse=True)
def clear_update_cache(monkeypatch) -> None:
    monkeypatch.setattr(app_updates, "_cached_result", None)


def test_version_comparison_handles_stable_prerelease_and_build_metadata() -> None:
    assert app_updates._is_newer("v0.2.0-beta.10", "0.2.0-beta.3") is True
    assert app_updates._is_newer("0.2.0", "0.2.0-beta.10") is True
    assert app_updates._is_newer("0.2.0-beta.3", "0.2.0") is False
    assert app_updates._is_newer("0.2", "0.2.0") is False
    assert app_updates._is_newer("1.0.0+build.2", "1.0.0+build.1") is False
    assert app_updates._is_newer("not-a-version", "0.2.0") is False


def test_stable_build_ignores_prereleases_when_selecting_latest() -> None:
    release = app_updates._parse_release_feed(
        _feed(
            ("v0.3.0-beta.2", "2026-08-05T12:00:00Z"),
            ("v0.2.1", "2026-08-04T12:00:00Z"),
        ),
        "0.2.0",
    )

    assert release is not None
    assert release.version == "0.2.1"


def test_supported_stable_line_ignores_legacy_prerelease_tags() -> None:
    release = app_updates._parse_release_feed(
        _feed(
            ("v0.12-beta.2", "2026-07-31T06:13:44Z"),
            ("v0.2.1", "2026-08-05T12:00:00Z"),
        ),
        "0.2.0",
    )

    assert release is not None
    assert release.version == "0.2.1"
    assert app_updates._is_newer(release.version, "0.2.0") is True


def test_prerelease_build_can_advance_to_newer_prerelease_or_stable() -> None:
    release = app_updates._parse_release_feed(
        _feed(
            ("v0.3.0-beta.2", "2026-08-05T12:00:00Z"),
            ("v0.2.1", "2026-08-04T12:00:00Z"),
        ),
        "0.3.0-beta.1",
    )

    assert release is not None
    assert release.version == "0.3.0-beta.2"


def test_fetch_version_uses_validated_github_release_url(monkeypatch) -> None:
    """Catch regressions that stop returning a validated configured-repository release link."""
    monkeypatch.setattr(app_updates, "app_version", lambda: "0.2.0")
    monkeypatch.setattr(app_updates, "update_check_enabled", lambda: True)
    monkeypatch.setattr(
        app_updates,
        "release_repository",
        lambda: "OpenBMB/StaffDeck",
        raising=False,
    )
    monkeypatch.setattr(
        app_updates.httpx,
        "get",
        lambda *args, **kwargs: _response(
            _feed(("v0.2.1", "2026-08-05T12:00:00Z"))
        ),
    )

    result = app_updates._fetch_version()

    assert result.current_version == "0.2.0"
    assert result.latest_version == "0.2.1"
    assert result.update_available is True
    assert result.release_url.endswith("/releases/tag/v0.2.1")
    assert result.check_succeeded is True


def test_source_deployment_skips_network_check_by_default(monkeypatch) -> None:
    monkeypatch.setattr(app_updates, "app_version", lambda: "0.2.0")
    monkeypatch.setattr(app_updates, "update_check_enabled", lambda: False)
    monkeypatch.setattr(
        app_updates.httpx,
        "get",
        lambda *args, **kwargs: pytest.fail("disabled checks must not access GitHub"),
    )

    result = app_updates._fetch_version()

    assert result.check_enabled is False
    assert result.check_succeeded is False
    assert result.update_available is False


def test_fetch_version_fails_silently_and_failure_is_cached(monkeypatch) -> None:
    """Catch repeated network calls or surfaced errors after a cached feed failure."""
    calls = 0

    def fail(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(app_updates, "app_version", lambda: "0.2.0")
    monkeypatch.setattr(app_updates, "update_check_enabled", lambda: True)
    monkeypatch.setattr(
        app_updates,
        "release_repository",
        lambda: "OpenBMB/StaffDeck",
        raising=False,
    )
    monkeypatch.setattr(app_updates.httpx, "get", fail)

    first = app_updates.get_app_version()
    second = app_updates.get_app_version()

    assert first.check_succeeded is False
    assert first.update_available is False
    assert second == first
    assert calls == 1


def test_release_tag_rejects_untrusted_alternate_link() -> None:
    """Catch off-domain alternate links being salvaged into a trusted-looking release entry."""
    content = b"""<feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <id>tag:github.com,2008:Repository/1/v0.2.1</id>
        <link rel="alternate" href="https://example.com/releases/tag/v9.9.9" />
        <title>Untrusted link</title>
      </entry>
    </feed>"""

    release = app_updates._parse_release_feed(content, "0.2.0")

    assert release is None


def test_fetch_version_uses_packaged_repository_for_feed_and_release_url(monkeypatch) -> None:
    """Catch a Fork package querying or linking to the upstream release repository."""
    requested_urls: list[str] = []

    def fetch(url: str, **kwargs) -> httpx.Response:
        """Record the real boundary argument while returning a complete matching feed response."""
        requested_urls.append(url)
        return httpx.Response(
            200,
            content=_feed(
                ("v0.2.1", "2026-08-05T12:00:00Z"),
                repository="JnyRoad/StaffDeck",
            ),
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(app_updates, "app_version", lambda: "0.2.0")
    monkeypatch.setattr(app_updates, "update_check_enabled", lambda: True)
    monkeypatch.setattr(
        app_updates,
        "release_repository",
        lambda: "JnyRoad/StaffDeck",
        raising=False,
    )
    monkeypatch.setattr(app_updates.httpx, "get", fetch)

    result = app_updates._fetch_version()

    assert requested_urls == ["https://github.com/JnyRoad/StaffDeck/releases.atom"]
    assert result.release_repository == "JnyRoad/StaffDeck"
    assert result.release_url == "https://github.com/JnyRoad/StaffDeck/releases/tag/v0.2.1"
    assert result.update_available is True
    assert result.check_succeeded is True


def test_fetch_version_rejects_release_entry_from_other_repository(monkeypatch) -> None:
    """Catch a valid GitHub tag from another repository becoming an actionable update."""
    monkeypatch.setattr(app_updates, "app_version", lambda: "0.2.0")
    monkeypatch.setattr(app_updates, "update_check_enabled", lambda: True)
    monkeypatch.setattr(
        app_updates,
        "release_repository",
        lambda: "JnyRoad/StaffDeck",
        raising=False,
    )
    monkeypatch.setattr(
        app_updates.httpx,
        "get",
        lambda url, **kwargs: httpx.Response(
            200,
            content=_feed(("v9.9.9", "2026-08-05T12:00:00Z")),
            request=httpx.Request("GET", url),
        ),
    )

    result = app_updates._fetch_version()

    assert result.release_repository == "JnyRoad/StaffDeck"
    assert result.release_url == ""
    assert result.update_available is False
    assert result.check_succeeded is False


def test_fetch_version_skips_network_when_distribution_identity_is_invalid(monkeypatch) -> None:
    """Catch a frozen package without trusted metadata falling back to any network release feed."""
    monkeypatch.setattr(app_updates, "app_version", lambda: "0.2.0")
    monkeypatch.setattr(app_updates, "update_check_enabled", lambda: True)
    monkeypatch.setattr(app_updates, "release_repository", lambda: None, raising=False)
    monkeypatch.setattr(
        app_updates.httpx,
        "get",
        lambda *args, **kwargs: pytest.fail("invalid identity must not access GitHub"),
    )

    result = app_updates._fetch_version()

    assert result.release_repository is None
    assert result.release_url == ""
    assert result.update_available is False
    assert result.check_succeeded is False


def test_app_version_priority_and_macos_resources(monkeypatch, tmp_path: Path) -> None:
    app_root = tmp_path / "StaffDeck.app"
    executable = app_root / "Contents" / "MacOS" / "staffdeck"
    resource = app_root / "Contents" / "Resources" / "staffdeck-version.txt"
    executable.parent.mkdir(parents=True)
    resource.parent.mkdir(parents=True)
    resource.write_text("0.2.1\n", encoding="utf-8")

    monkeypatch.delattr(version.sys, "_MEIPASS", raising=False)
    monkeypatch.setattr(version.sys, "frozen", True, raising=False)
    monkeypatch.setattr(version.sys, "platform", "darwin")
    monkeypatch.setattr(version.sys, "executable", str(executable))
    monkeypatch.delenv("STAFFDECK_VERSION", raising=False)

    assert version.app_version() == "0.2.1"
    monkeypatch.setenv("STAFFDECK_VERSION", "0.3.0")
    assert version.app_version() == "0.3.0"


@pytest.mark.parametrize(
    ("configured", "frozen", "expected"),
    [("true", False, True), ("false", True, False), ("", True, True), ("", False, False)],
)
def test_update_check_enablement(monkeypatch, configured: str, frozen: bool, expected: bool) -> None:
    if configured:
        monkeypatch.setenv("STAFFDECK_UPDATE_CHECK", configured)
    else:
        monkeypatch.delenv("STAFFDECK_UPDATE_CHECK", raising=False)
    monkeypatch.setattr(version.sys, "frozen", frozen, raising=False)
    assert version.update_check_enabled() is expected
