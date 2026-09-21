from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class APIResponse:
    """JSON is preserved, including fields added by newer compatible servers."""

    data: Any
    status_code: int
    request_id: str | None = None
    etag: str | None = None


@dataclass(frozen=True)
class RunEvent:
    id: str
    event: str
    data: Any
