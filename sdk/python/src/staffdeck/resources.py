from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from .models import APIResponse

if TYPE_CHECKING:
    from .client import StaffDeck


def segment(value: str) -> str:
    if (
        not value or value in {".", ".."} or any(c in value for c in "/\\%")
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        raise ValueError("Resource ID must be a nonempty, safe path segment.")
    return quote(value, safe="")


def agent_path(agent_id: str, resource: str = "") -> str:
    return f"agents/{segment(agent_id)}" + (f"/{resource}" if resource else "")


class Resource:
    def __init__(self, client: StaffDeck) -> None:
        self._client = client


class Agents(Resource):
    def list(self, *, limit: int = 50) -> APIResponse:
        return self._client.request("GET", "agents", params={"limit": limit})

    def get(self, agent_id: str) -> APIResponse:
        return self._client.request("GET", agent_path(agent_id))

    def create(self, body: dict[str, Any], *, idempotency_key: str | None = None) -> APIResponse:
        return self._client.request("POST", "agents", body=body, idempotency_key=idempotency_key)

    def update(self, agent_id: str, body: dict[str, Any], *, if_match: str) -> APIResponse:
        return self._client.request("PATCH", agent_path(agent_id), body=body, if_match=if_match)

    def capabilities(self, agent_id: str) -> APIResponse:
        return self._client.request("GET", agent_path(agent_id, "capabilities"))

    def resources(self, agent_id: str) -> APIResponse:
        return self._client.request("GET", agent_path(agent_id, "resources"))

    def set_resources(self, agent_id: str, resources: list[dict[str, Any]]) -> APIResponse:
        return self._client.request(
            "PUT", agent_path(agent_id, "resources"), body={"resources": resources}
        )


class Sessions(Resource):
    def list(self, agent_id: str, *, limit: int = 50) -> APIResponse:
        return self._client.request(
            "GET", agent_path(agent_id, "sessions"), params={"limit": limit}
        )

    def create(
        self, agent_id: str, body: dict[str, Any] | None = None,
        *, idempotency_key: str | None = None,
    ) -> APIResponse:
        return self._client.request(
            "POST", agent_path(agent_id, "sessions"), body=body or {},
            idempotency_key=idempotency_key,
        )

    def get(self, agent_id: str, session_id: str) -> APIResponse:
        return self._client.request("GET", agent_path(agent_id, f"sessions/{segment(session_id)}"))

    def update(
        self, agent_id: str, session_id: str, body: dict[str, Any], *, if_match: str,
    ) -> APIResponse:
        return self._client.request(
            "PATCH", agent_path(agent_id, f"sessions/{segment(session_id)}"),
            body=body, if_match=if_match,
        )


class Tools(Resource):
    def list(self, agent_id: str) -> APIResponse:
        return self._client.request("GET", agent_path(agent_id, "tools"))

    def create(self, agent_id: str, body: dict[str, Any]) -> APIResponse:
        return self._client.request("POST", agent_path(agent_id, "tools"), body=body)

    def update(self, agent_id: str, tool_id: str, body: dict[str, Any]) -> APIResponse:
        return self._client.request(
            "PUT", agent_path(agent_id, f"tools/{segment(tool_id)}"), body=body
        )

    def test(self, agent_id: str, tool_id: str, body: dict[str, Any]) -> APIResponse:
        return self._client.request(
            "POST", agent_path(agent_id, f"tools/{segment(tool_id)}:test"), body=body
        )


class MCPServers(Resource):
    def list(self, agent_id: str) -> APIResponse:
        return self._client.request("GET", agent_path(agent_id, "mcp-servers"))

    def create(self, agent_id: str, body: dict[str, Any]) -> APIResponse:
        return self._client.request("POST", agent_path(agent_id, "mcp-servers"), body=body)

    def update(self, agent_id: str, server_id: str, body: dict[str, Any]) -> APIResponse:
        return self._client.request(
            "PUT", agent_path(agent_id, f"mcp-servers/{segment(server_id)}"), body=body
        )

    def discover(self, agent_id: str, server_id: str) -> APIResponse:
        return self._client.request(
            "POST", agent_path(agent_id, f"mcp-servers/{segment(server_id)}:discover")
        )

    def sync(self, agent_id: str, server_id: str, body: dict[str, Any]) -> APIResponse:
        return self._client.request(
            "POST", agent_path(agent_id, f"mcp-servers/{segment(server_id)}:sync"), body=body
        )


class SOPs(Resource):
    def list(self, agent_id: str) -> APIResponse:
        return self._client.request("GET", agent_path(agent_id, "sops"))

    def create(
        self, agent_id: str, content: dict[str, Any], *, idempotency_key: str | None = None,
    ) -> APIResponse:
        return self._client.request(
            "POST", agent_path(agent_id, "sops"), body={"content": content},
            idempotency_key=idempotency_key,
        )

    def get_draft(self, agent_id: str, sop_id: str, draft_id: str) -> APIResponse:
        return self._client.request(
            "GET", agent_path(agent_id, f"sops/{segment(sop_id)}/drafts/{segment(draft_id)}")
        )

    def replace(
        self, agent_id: str, sop_id: str, content: dict[str, Any],
        *, draft_id: str, if_match: str,
    ) -> APIResponse:
        return self._client.request(
            "PUT", agent_path(agent_id, f"sops/{segment(sop_id)}"),
            body={"content": content}, params={"draft_id": draft_id}, if_match=if_match,
        )

    def patch(
        self, agent_id: str, sop_id: str, operations: list[dict[str, Any]],
        *, draft_id: str, if_match: str,
    ) -> APIResponse:
        return self._client.request(
            "PATCH", agent_path(agent_id, f"sops/{segment(sop_id)}"), body=operations,
            params={"draft_id": draft_id}, if_match=if_match,
            content_type="application/json-patch+json",
        )

    def validate(self, agent_id: str, sop_id: str, draft_id: str) -> APIResponse:
        return self._client.request(
            "POST", f"sops/{segment(sop_id)}:validate",
            params={"agent_id": agent_id, "draft_id": draft_id},
        )

    def publish(self, agent_id: str, sop_id: str, draft_id: str) -> APIResponse:
        return self._client.request(
            "POST", f"sops/{segment(sop_id)}:publish", params={"agent_id": agent_id},
            body={"draft_id": draft_id},
        )

    def versions(self, agent_id: str, sop_id: str) -> APIResponse:
        return self._client.request(
            "GET", f"sops/{segment(sop_id)}/versions", params={"agent_id": agent_id}
        )

    def rollback(self, agent_id: str, sop_id: str, version: str) -> APIResponse:
        """Creates a private draft; does not publish it."""
        return self._client.request(
            "POST", f"sops/{segment(sop_id)}/versions/{segment(version)}:rollback",
            params={"agent_id": agent_id},
        )
