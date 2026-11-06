from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any

from .models import APIResponse
from .resources import Resource, agent_path, segment

MAX_DOCUMENT_BYTES = 20 * 1024 * 1024


def base_path(agent_id: str, knowledge_base_id: str) -> str:
    return agent_path(agent_id, f"knowledge-bases/{segment(knowledge_base_id)}")


class KnowledgeBases(Resource):
    def list(self, agent_id: str) -> APIResponse:
        return self._client.request("GET", agent_path(agent_id, "knowledge-bases"))

    def create(self, agent_id: str, body: dict[str, Any]) -> APIResponse:
        return self._client.request("POST", agent_path(agent_id, "knowledge-bases"), body=body)

    def update(
        self, agent_id: str, knowledge_base_id: str, body: dict[str, Any],
    ) -> APIResponse:
        return self._client.request("PATCH", base_path(agent_id, knowledge_base_id), body=body)

    def archive(self, agent_id: str, knowledge_base_id: str) -> APIResponse:
        return self._client.request("POST", base_path(agent_id, knowledge_base_id) + ":archive")

    def search(
        self, agent_id: str, knowledge_base_id: str, body: dict[str, Any],
    ) -> APIResponse:
        return self._client.request(
            "POST", base_path(agent_id, knowledge_base_id) + ":search", body=body,
        )

    def upsert_entries(
        self, agent_id: str, knowledge_base_id: str, body: dict[str, Any],
        *, idempotency_key: str | None = None,
    ) -> APIResponse:
        """Submit text entries; use jobs.wait(receipt.data['id']) before searching."""
        return self._client.request(
            "POST", base_path(agent_id, knowledge_base_id) + "/entries",
            body=body, idempotency_key=idempotency_key,
        )

    def upload_document(
        self, agent_id: str, knowledge_base_id: str, file_path: str | Path,
        *, title: str | None = None,
    ) -> APIResponse:
        """Upload one local file, once. The server does not deduplicate uploads."""
        path = base_path(agent_id, knowledge_base_id) + "/documents"
        source = Path(file_path)
        try:
            if not source.is_file():
                raise ValueError("Document must be a readable regular file.")
            with source.open("rb") as stream:
                content = stream.read(MAX_DOCUMENT_BYTES + 1)
        except OSError:
            raise ValueError("Cannot read document file.") from None
        if len(content) > MAX_DOCUMENT_BYTES:
            raise ValueError("Documents are limited to 20 MB.")
        media_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
        return self._client.request(
            "POST", path, files={"file": (source.name, content, media_type)},
            form={"title": title} if title is not None else {},
        )

    def documents(self, agent_id: str, knowledge_base_id: str) -> APIResponse:
        return self._client.request("GET", base_path(agent_id, knowledge_base_id) + "/documents")

    def update_document(
        self, agent_id: str, knowledge_base_id: str, document_id: str, body: dict[str, Any],
    ) -> APIResponse:
        return self._client.request(
            "PATCH", base_path(agent_id, knowledge_base_id) + f"/documents/{segment(document_id)}",
            body=body,
        )

    def archive_document(
        self, agent_id: str, knowledge_base_id: str, document_id: str,
    ) -> APIResponse:
        return self._client.request(
            "POST", base_path(agent_id, knowledge_base_id)
            + f"/documents/{segment(document_id)}:archive",
        )

    def versions(self, agent_id: str, knowledge_base_id: str) -> APIResponse:
        return self._client.request("GET", base_path(agent_id, knowledge_base_id) + "/versions")

    def rollback(self, agent_id: str, knowledge_base_id: str, version: str) -> APIResponse:
        """Immediately change the employee's knowledge version binding."""
        return self._client.request(
            "POST", base_path(agent_id, knowledge_base_id) + ":rollback", body={"version": version},
        )

    def concepts(self, agent_id: str, knowledge_base_id: str) -> APIResponse:
        return self._client.request("GET", base_path(agent_id, knowledge_base_id) + "/concepts")
