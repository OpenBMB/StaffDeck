from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree

import httpx
from fastapi import APIRouter
from packaging.version import InvalidVersion, Version
from pydantic import BaseModel

from app.version import app_version, update_check_enabled

router = APIRouter(prefix="/api/app", tags=["app"])

RELEASES_FEED_URL = "https://github.com/OpenBMB/StaffDeck/releases.atom"
RELEASES_PAGE_URL = "https://github.com/OpenBMB/StaffDeck/releases"
RELEASE_TAG_PATH = "/OpenBMB/StaffDeck/releases/tag/"
SUCCESS_CACHE_SECONDS = 6 * 60 * 60
FAILURE_CACHE_SECONDS = 15 * 60

_cache_lock = threading.Lock()


class AppVersionRead(BaseModel):
    current_version: str
    latest_version: str | None = None
    update_available: bool = False
    release_url: str = RELEASES_PAGE_URL
    release_name: str | None = None
    published_at: str | None = None
    check_enabled: bool = True
    check_succeeded: bool = True


_cached_result: tuple[float, AppVersionRead] | None = None


@dataclass(frozen=True)
class ReleaseEntry:
    version: str
    parsed_version: Version
    name: str
    url: str
    published_at: str | None


def _parse_version(value: str) -> Version | None:
    normalized = value.strip()
    if normalized[:1].lower() == "v":
        normalized = normalized[1:]
    normalized = normalized.partition("+")[0]
    try:
        return Version(normalized)
    except InvalidVersion:
        return None


def _is_newer(latest: str, current: str) -> bool:
    latest_version = _parse_version(latest)
    current_version = _parse_version(current)
    return bool(
        latest_version is not None
        and current_version is not None
        and latest_version > current_version
    )


def _release_tag(entry: ElementTree.Element, namespace: dict[str, str]) -> tuple[str, str]:
    link = entry.find("atom:link[@rel='alternate']", namespace)
    release_url = link.get("href", "").strip() if link is not None else ""
    if release_url:
        parsed_url = urlsplit(release_url)
        path = unquote(parsed_url.path)
        if (
            parsed_url.scheme == "https"
            and parsed_url.netloc == "github.com"
            and path.startswith(RELEASE_TAG_PATH)
        ):
            tag = path.removeprefix(RELEASE_TAG_PATH).strip("/")
            if _parse_version(tag) is not None:
                return tag, release_url

    entry_id = (entry.findtext("atom:id", default="", namespaces=namespace) or "").strip()
    tag = unquote(entry_id.rsplit("/", 1)[-1]) if "/" in entry_id else ""
    if _parse_version(tag) is not None:
        return tag, RELEASES_PAGE_URL
    return "", RELEASES_PAGE_URL


def _parse_release_feed(content: bytes, current: str) -> ReleaseEntry | None:
    root = ElementTree.fromstring(content)
    namespace = {"atom": "http://www.w3.org/2005/Atom"}
    current_version = _parse_version(current)
    if current_version is None:
        return None
    releases: list[ReleaseEntry] = []
    for entry in root.findall("atom:entry", namespace):
        tag, release_url = _release_tag(entry, namespace)
        parsed = _parse_version(tag)
        if parsed is None:
            continue
        # Stable update guarantees start at 0.2.0. Stable builds never consider
        # older prerelease tags, including the historical 0.12-beta release line.
        if not current_version.is_prerelease and parsed.is_prerelease:
            continue
        title = (entry.findtext("atom:title", default="", namespaces=namespace) or "").strip()
        releases.append(
            ReleaseEntry(
                version=tag.removeprefix("v").removeprefix("V"),
                parsed_version=parsed,
                name=title or tag,
                url=release_url,
                published_at=entry.findtext("atom:updated", default=None, namespaces=namespace),
            )
        )
    return max(releases, key=lambda item: item.parsed_version, default=None)


def _fetch_version() -> AppVersionRead:
    current = app_version()
    if not update_check_enabled():
        return AppVersionRead(
            current_version=current,
            check_enabled=False,
            check_succeeded=False,
        )
    try:
        response = httpx.get(
            RELEASES_FEED_URL,
            headers={"Accept": "application/atom+xml", "User-Agent": "StaffDeck"},
            timeout=4.0,
            follow_redirects=True,
        )
        response.raise_for_status()
        release = _parse_release_feed(response.content, current)
        if release is None:
            return AppVersionRead(current_version=current, check_succeeded=False)
        return AppVersionRead(
            current_version=current,
            latest_version=release.version,
            update_available=_is_newer(release.version, current),
            release_url=release.url,
            release_name=release.name,
            published_at=release.published_at,
        )
    except (httpx.HTTPError, ElementTree.ParseError, ValueError, TypeError):
        return AppVersionRead(current_version=current, check_succeeded=False)


@router.get("/version", response_model=AppVersionRead)
def get_app_version() -> AppVersionRead:
    global _cached_result
    now = time.monotonic()
    with _cache_lock:
        if _cached_result is not None and _cached_result[0] > now:
            return _cached_result[1]
        result = _fetch_version()
        ttl = SUCCESS_CACHE_SECONDS if result.check_succeeded else FAILURE_CACHE_SECONDS
        _cached_result = (now + ttl, result)
        return result
