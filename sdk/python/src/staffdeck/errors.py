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


class RunFailedError(StaffDeckError):
    def __init__(self, run_id: str, job: dict[str, Any]) -> None:
        super().__init__("StaffDeck run did not succeed; inspect the job for details.")
        self.run_id = run_id
        self.job = job
        self.status = job.get("status")


class WaitTimeout(StaffDeckError):
    def __init__(self, run_id: str) -> None:
        super().__init__("Timed out waiting for StaffDeck; the run was not cancelled.")
        self.run_id = run_id


class StreamError(StaffDeckError):
    def __init__(self, run_id: str, last_event_id: str | None, message: str) -> None:
        super().__init__(message)
        self.run_id = run_id
        self.last_event_id = last_event_id

