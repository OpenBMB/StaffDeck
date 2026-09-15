from __future__ import annotations

from typing import Any


class StaffDeckError(Exception):
    """Base error; exception messages never include request/response bodies."""


class APIError(StaffDeckError):
    def __init__(
        self,
        status_code: int,
        *,
        problem: dict[str, Any] | None = None,
        request_id: str | None = None,
        retry_after: str | None = None,
    ) -> None:
        super().__init__(f"StaffDeck API returned HTTP {status_code}.")
        self.status_code = status_code
        self.problem = problem or {}
        self.code = self.problem.get("code", "HTTP_ERROR")
        self.detail = self.problem.get("detail", "")
        self.errors = self.problem.get("errors", [])
        self.request_id = request_id or self.problem.get("request_id")
        self.retry_after = retry_after


class TransportError(StaffDeckError):
    def __init__(self) -> None:
        super().__init__("StaffDeck request failed in transit; a write may have been applied.")


class ProtocolError(StaffDeckError):
    """The response is not compatible with the expected public API protocol."""

