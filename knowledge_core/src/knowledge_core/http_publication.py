from typing import Any

import httpx


class HttpSuggestionPublisher:
    def __init__(self, url: str, bearer: str | None = None):
        self.url = url
        self.headers = {"Authorization": f"Bearer {bearer}"} if bearer else {}

    def publish(self, suggestion: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        response = httpx.post(
            self.url, headers={**self.headers, "Idempotency-Key": idempotency_key},
            json={"suggestion": suggestion}, timeout=120,
        )
        response.raise_for_status()
        return response.json()
