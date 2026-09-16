from __future__ import annotations

import math
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Self
from urllib.parse import unquote

import httpx

from .errors import APIError, ProtocolError, TransportError
from .models import APIResponse
from .resources import Agents, MCPServers, Sessions, SOPs, Tools
from .runs import Runs

_RETRY_STATUSES = {429, 502, 503, 504}


def _positive(value: float, name: str) -> float:
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a finite positive number.")
    return value


def _header(value: str, name: str) -> str:
    if not value or not value.isascii() or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError(f"{name} must be a nonempty printable ASCII string.")
    return value


def _base_url(value: str) -> httpx.URL:
    try:
        url = httpx.URL(value)
    except httpx.InvalidURL:
        raise ValueError("base_url must be a valid HTTP(S) URL.") from None
    if (
        url.scheme not in {"http", "https"}
        or not url.host
        or url.userinfo
        or url.query
        or url.fragment
    ):
        raise ValueError("base_url must be an HTTP(S) URL without credentials, query or fragment.")
    path = url.path.rstrip("/") or "/api/v1"
    if not path.endswith("/api/v1"):
        raise ValueError("base_url must end in /api/v1 or be a bare server origin.")
    return url.copy_with(path=path + "/")


class StaffDeck:
    """Synchronous client. Use a context manager or call close() when finished."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout: float = 30.0,
        max_retries: int = 2,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.timeout = _positive(timeout, "timeout")
        if not isinstance(max_retries, int) or not 0 <= max_retries <= 10:
            raise ValueError("max_retries must be an integer between 0 and 10.")
        self.max_retries = max_retries
        self._http = httpx.Client(
            base_url=_base_url(base_url),
            headers={
                "Authorization": f"Bearer {_header(api_key.strip(), 'api_key')}",
                "Accept": "application/json",
                "User-Agent": "staffdeck-python/0.1.0",
            },
            timeout=self.timeout,
            follow_redirects=False,
            transport=transport,
        )
        self.agents = Agents(self)
        self.sessions = Sessions(self)
        self.tools = Tools(self)
        self.mcp_servers = MCPServers(self)
        self.sops = SOPs(self)
        self.runs = Runs(self)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def _path(self, path: str) -> str:
        # Never send the bearer token to a caller-supplied host or outside the API prefix.
        if not path or path.startswith("/") or any(c in path for c in "?#\\"):
            raise ValueError("API path must be relative and must not contain a query or fragment.")
        decoded = unquote(path)
        if (
            ":" in decoded.split("/", 1)[0]
            or decoded.startswith("/")
            or "\\" in decoded
            or "%" in decoded
            or any(ord(c) < 32 or ord(c) == 127 for c in decoded)
            or any(part in {".", ".."} for part in decoded.split("/"))
        ):
            raise ValueError("Unsafe API path.")
        url = self._http.base_url.join(path)
        if (
            url.scheme != self._http.base_url.scheme
            or url.netloc != self._http.base_url.netloc
            or not url.path.startswith(self._http.base_url.path)
        ):
            raise ValueError("API path must remain within base_url.")
        return path

    @staticmethod
    def _check_response(response: httpx.Response) -> None:
        if response.is_success:
            return
        try:
            problem = response.json()
        except ValueError:
            problem = None
        raise APIError(
            response.status_code,
            problem=problem if isinstance(problem, dict) else None,
            request_id=response.headers.get("X-Request-ID"),
            retry_after=response.headers.get("Retry-After"),
        )

    @staticmethod
    def _retry_delay(attempt: int, retry_after: str | None = None) -> float | None:
        delay = min(0.5 * 2**attempt, 8.0)
        if retry_after:
            try:
                seconds = float(retry_after)
            except ValueError:
                try:
                    date = parsedate_to_datetime(retry_after)
                    seconds = (date - datetime.now(UTC)).total_seconds()
                except (ValueError, TypeError, OverflowError):
                    return delay
            # Do not retry earlier than requested, or sleep indefinitely on untrusted headers.
            if not math.isfinite(seconds) or seconds > 30:
                return None
            delay = max(delay, seconds)
        return delay

    def request(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        params: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        if_match: str | None = None,
        content_type: str = "application/json",
        timeout: float | None = None,
        retry: bool = True,
    ) -> APIResponse:
        """Call a JSON API. Mutations are never automatically retried."""
        method = method.upper()
        if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
            raise ValueError("Unsupported HTTP method.")
        path = self._path(path)
        if isinstance(body, dict) and "tenant_id" in body:
            raise ValueError("The public API derives tenant_id from the credential.")
        headers = {"Content-Type": _header(content_type, "content_type")}
        if idempotency_key is not None:
            headers["Idempotency-Key"] = _header(idempotency_key, "idempotency_key")
            if len(idempotency_key) > 200:
                raise ValueError("idempotency_key must be at most 200 characters.")
        if if_match is not None:
            headers["If-Match"] = _header(if_match, "if_match")
        attempts = self.max_retries if method == "GET" and retry else 0
        request_timeout = self.timeout if timeout is None else _positive(timeout, "timeout")
        for attempt in range(attempts + 1):
            try:
                response = self._http.request(
                    method, path, json=body, params=params, headers=headers, timeout=request_timeout
                )
            except httpx.DecodingError:
                raise ProtocolError("Invalid compressed response from StaffDeck.") from None
            except httpx.TransportError:
                if attempt == attempts:
                    raise TransportError() from None
                time.sleep(self._retry_delay(attempt) or 0)
                continue
            with closing(response):
                delay = self._retry_delay(attempt, response.headers.get("Retry-After"))
                if (
                    response.status_code in _RETRY_STATUSES
                    and attempt < attempts
                    and delay is not None
                ):
                    response.close()
                    time.sleep(delay)
                    continue
                self._check_response(response)
                try:
                    data = response.json() if response.content else None
                except ValueError:
                    raise ProtocolError("Expected a JSON response from StaffDeck.") from None
                return APIResponse(
                    data=data,
                    status_code=response.status_code,
                    request_id=response.headers.get("X-Request-ID"),
                    etag=response.headers.get("ETag"),
                )
        raise AssertionError("Unreachable retry state")

    @contextmanager
    def _event_response(self, path: str, last_event_id: str | None) -> Iterator[httpx.Response]:
        headers = {"Accept": "text/event-stream"}
        if last_event_id is not None:
            headers["Last-Event-ID"] = _header(last_event_id, "last_event_id")
        with self._http.stream("GET", self._path(path), headers=headers) as response:
            if not response.is_success:
                response.read()
                self._check_response(response)
            if response.headers.get("Content-Type", "").split(";", 1)[0] != "text/event-stream":
                raise ProtocolError("Expected a text/event-stream response from StaffDeck.")
            response.encoding = "utf-8"
            yield response
