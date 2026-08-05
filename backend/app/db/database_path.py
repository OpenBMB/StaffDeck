from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote

from app import paths


def normalize_database_url(url: str) -> str:
    if (
        not url.startswith("sqlite:///")
        or url.startswith("sqlite:////")
        or url == "sqlite:///:memory:"
    ):
        return url
    raw_path = unquote(url.removeprefix("sqlite:///"))
    if not raw_path or raw_path == ":memory:":
        return url
    path = Path(raw_path)
    if path.is_absolute():
        return url
    base_dir = (
        paths.user_data_dir()
        if paths.is_frozen()
        else Path(__file__).resolve().parents[2]
    )
    return f"sqlite:///{(base_dir / path).resolve()}"


def sqlite_database_path(url: str) -> Path | None:
    normalized = normalize_database_url(url)
    if not normalized.startswith("sqlite:///") or normalized == "sqlite:///:memory:":
        return None
    raw_path = unquote(normalized.removeprefix("sqlite:///"))
    return Path(raw_path).expanduser().resolve()


__all__ = ["normalize_database_url", "sqlite_database_path"]
