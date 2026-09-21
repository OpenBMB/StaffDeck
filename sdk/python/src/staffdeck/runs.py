from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any

import httpx

from .errors import (
    APIError,
    ProtocolError,
    RunFailedError,
    StreamError,
    TransportError,
    WaitTimeout,
)
from .models import APIResponse, RunEvent
from .resources import Resource, agent_path, segment
from .streaming import IncompleteEvent, is_sequence_id, iter_sse_lines, parse_events

_TERMINAL = {"succeeded", "failed", "cancelled"}


def _status(data: Any) -> str:
    status = data.get("status") if isinstance(data, dict) else None
    if not isinstance(status, str) or status not in _TERMINAL | {"queued", "running"}:
        raise ProtocolError("Run response has a missing or unsupported status.")
    return status


class Runs(Resource):
    def create(
        self, agent_id: str, body: dict[str, Any], *, idempotency_key: str | None = None,
    ) -> APIResponse:
        """Return a durable job receipt, not a completed run result."""
        return self._client.request(
            "POST", agent_path(agent_id, "runs"), body=body, idempotency_key=idempotency_key
        )

    def get(self, run_id: str) -> APIResponse:
        return self._client.request("GET", f"runs/{segment(run_id)}")

    def result(self, run_id: str) -> APIResponse:
        return self._client.request("GET", f"runs/{segment(run_id)}/result")

    def cancel(self, run_id: str) -> APIResponse:
        """Request cancellation; query status to observe the eventual terminal state."""
        return self._client.request("POST", f"runs/{segment(run_id)}:cancel")

    def wait(
        self, run_id: str, *, timeout: float = 300.0, poll_interval: float = 1.0,
    ) -> APIResponse:
        """Wait for success and return its result. Does not cancel on timeout."""
        from .client import _positive

        deadline = time.monotonic() + _positive(timeout, "timeout")
        _positive(poll_interval, "poll_interval")
        path = f"runs/{segment(run_id)}"
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WaitTimeout(run_id)
            response = self._client.request(
                "GET", path, timeout=min(self._client.timeout, remaining), retry=False
            )
            status = _status(response.data)
            if status in {"failed", "cancelled"}:
                raise RunFailedError(run_id, response.data)
            if status == "succeeded":
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise WaitTimeout(run_id)
                return self._client.request(
                    "GET", path + "/result",
                    timeout=min(self._client.timeout, remaining), retry=False,
                )
            time.sleep(min(poll_interval, max(0, deadline - time.monotonic())))

    def events(
        self, run_id: str, *, last_event_id: str | None = None, max_reconnects: int = 2,
    ) -> Iterator[RunEvent]:
        """Consume a run's persisted events. Reconnect only GET, never recreate a run.

        Close the iterator (or use contextlib.closing) when stopping early. Save
        each processed event.id to resume from another process.
        """
        if not isinstance(max_reconnects, int) or not 0 <= max_reconnects <= 10:
            raise ValueError("max_reconnects must be an integer between 0 and 10.")
        if last_event_id is not None and not is_sequence_id(last_event_id):
            raise ValueError("last_event_id must be a numeric sequence ID.")
        path = f"runs/{segment(run_id)}/events"
        cursor = last_event_id
        for attempt in range(max_reconnects + 1):
            delay = self._client._retry_delay(attempt)
            try:
                with self._client._event_response(path, cursor) as response:
                    for event in parse_events(iter_sse_lines(response.iter_text())):
                        if cursor is not None and int(event.id) <= int(cursor):
                            continue
                        cursor = event.id
                        yield event
                # EOF alone is not success: proxies may close a healthy but active run.
                state = self.get(run_id).data
                status = _status(state)
                # Neither terminal-named process events nor repeated empty streams
                # prove delivery. Only the server's authoritative final cursor does.
                if status in _TERMINAL:
                    final_id = state.get("final_event_id")
                    if final_id is not None:
                        if not is_sequence_id(final_id):
                            raise ProtocolError("Invalid final event ID in run response.")
                        if int(cursor or "0") == int(final_id):
                            return
                        if int(cursor or "0") > int(final_id):
                            raise ProtocolError("Event cursor exceeds the run's final event ID.")
                    # Older servers cannot certify completion. Resume within budget,
                    # then report StreamError rather than silently dropping the tail.
            except APIError as exc:
                if exc.status_code not in {429, 502, 503, 504}:
                    raise
                delay = self._client._retry_delay(attempt, exc.retry_after)
            except (httpx.TransportError, TransportError, IncompleteEvent):
                pass
            except (httpx.DecodingError, ProtocolError):
                raise StreamError(run_id, cursor, "Invalid StaffDeck event stream.") from None
            if attempt == max_reconnects or delay is None:
                raise StreamError(run_id, cursor, "StaffDeck event stream interrupted.") from None
            time.sleep(delay)
