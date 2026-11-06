"""Additional framework resources using the same authenticated HTTP client."""
from typing import Any

from .models import APIResponse
from .resources import Resource, agent_path, segment


class GeneralSkills(Resource):
    def list(self, agent_id: str) -> APIResponse:
        return self._client.request("GET", agent_path(agent_id, "general-skills"))

    def create(self, agent_id: str, body: dict[str, Any]) -> APIResponse:
        return self._client.request("POST", agent_path(agent_id, "general-skills"), body=body)

    def publish(self, agent_id: str, slug: str) -> APIResponse:
        return self._client.request(
            "POST", agent_path(agent_id, f"general-skills/{segment(slug)}:publish")
        )

    def archive(self, agent_id: str, slug: str) -> APIResponse:
        return self._client.request(
            "POST", agent_path(agent_id, f"general-skills/{segment(slug)}:archive")
        )

    def test(self, agent_id: str, slug: str, body: dict[str, Any]) -> APIResponse:
        return self._client.request(
            "POST", agent_path(agent_id, f"general-skills/{segment(slug)}:test"), body=body
        )


class ScheduledTasks(Resource):
    def list(self, agent_id: str) -> APIResponse:
        return self._client.request("GET", agent_path(agent_id, "scheduled-tasks"))

    def create(self, agent_id: str, body: dict[str, Any]) -> APIResponse:
        return self._client.request("POST", agent_path(agent_id, "scheduled-tasks"), body=body)

    def update(self, agent_id: str, task_id: str, body: dict[str, Any]) -> APIResponse:
        return self._client.request(
            "PATCH", agent_path(agent_id, f"scheduled-tasks/{segment(task_id)}"), body=body
        )

    def run(self, agent_id: str, task_id: str) -> APIResponse:
        return self._action(agent_id, task_id, "run")

    def pause(self, agent_id: str, task_id: str) -> APIResponse:
        return self._action(agent_id, task_id, "pause")

    def resume(self, agent_id: str, task_id: str) -> APIResponse:
        return self._action(agent_id, task_id, "resume")

    def archive(self, agent_id: str, task_id: str) -> APIResponse:
        return self._action(agent_id, task_id, "archive")

    def runs(self, agent_id: str, task_id: str) -> APIResponse:
        return self._client.request(
            "GET", agent_path(agent_id, f"scheduled-tasks/{segment(task_id)}/runs")
        )

    def _action(self, agent_id: str, task_id: str, action: str) -> APIResponse:
        return self._client.request(
            "POST", agent_path(agent_id, f"scheduled-tasks/{segment(task_id)}:{action}")
        )


class Gallery(Resource):
    def list(self, *, limit: int = 20, cursor: str | None = None,
             query: str | None = None) -> APIResponse:
        return self._client.request("GET", "gallery/agents", params={
            "limit": limit, **({"cursor": cursor} if cursor else {}),
            **({"query": query} if query else {}),
        })

    def get(self, agent_id: str) -> APIResponse:
        return self._client.request("GET", f"gallery/agents/{segment(agent_id)}")

    def add(self, agent_id: str, *, idempotency_key: str | None = None) -> APIResponse:
        return self._client.request(
            "POST", f"gallery/agents/{segment(agent_id)}:add", idempotency_key=idempotency_key
        )


class System(Resource):
    def health(self) -> APIResponse:
        return self._client.request("GET", "health")

    def schema(self) -> APIResponse:
        return self._client.request("GET", "openapi.json")

    def whoami(self) -> APIResponse:
        return self._client.request("GET", "auth/me")


class API(Resource):
    def request(self, method: str, path: str, *, body: dict | None = None,
                idempotency_key: str | None = None, if_match: str | None = None) -> APIResponse:
        method = method.upper()
        if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
            raise ValueError("Unsupported method.")
        return self._client.request(
            method, path, body=body, idempotency_key=idempotency_key, if_match=if_match
        )
