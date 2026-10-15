from __future__ import annotations

from typing import Any, Literal, TypedDict, overload

import httpx

from .contracts import (
    BaseCreate,
    BaseEdit,
    ConceptEdit,
    BindingAction,
    BindingEdit,
    Edit,
    LayerEdit,
    QueryRequest,
    SpaceCreate,
    Upload,
    VersionAction,
)
from .responses import (KnowledgeBase, KnowledgeBaseVersion, KnowledgeBinding, KnowledgeBranch,
                        KnowledgeBucket, KnowledgeChunk, KnowledgeConcept, KnowledgeDiscoverySuggestion,
                        KnowledgeDocument, KnowledgeIngestJob, Space)
from .schema import KnowledgeSearchResponse


class OkfImportResult(TypedDict):
    document_id: str
    concept_count: int


class ReviewResult(TypedDict):
    status: str
    published: bool


def entity(model, value):
    normalized = {
        key + "_json" if key + "_json" in model.model_fields else key: item
        for key, item in value.items()
    }
    return model.model_validate(normalized)


class KnowledgeClient:
    def __init__(
        self, base_url: str, *, bearer: str | None = None, timeout: float = 120
    ):
        self.http = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {bearer}"} if bearer else {},
            timeout=timeout,
        )

    def request(self, method: str, path: str, *, body=None, params=None) -> Any:
        response = self.http.request(
            method, "/api/km/v1" + path, json=body, params=params
        )
        response.raise_for_status()
        return response.json()

    def create_base(self, request: BaseCreate) -> KnowledgeBase:
        return entity(KnowledgeBase, self.request(
            "POST", "/knowledge-bases", body=request.model_dump(mode="json")
        ))

    def spaces(self) -> list[Space]:
        return [entity(Space, row) for row in self.request("GET", "/spaces")]

    def create_space(self, request: SpaceCreate) -> Space:
        return entity(
            Space, self.request("POST", "/spaces", body=request.model_dump(mode="json"))
        )

    def bases(self, space_id="local") -> list[KnowledgeBase]:
        return [
            entity(KnowledgeBase, row)
            for row in self.request(
                "GET", "/knowledge-bases", params={"space_id": space_id}
            )
        ]

    def base(self, identifier: str, space_id="local") -> KnowledgeBase:
        return entity(
            KnowledgeBase,
            self.request(
                "GET", f"/knowledge-bases/{identifier}", params={"space_id": space_id}
            ),
        )

    def edit_base(
        self, identifier: str, request: BaseEdit, space_id="local"
    ) -> KnowledgeBase:
        return entity(
            KnowledgeBase,
            self.request(
                "PUT",
                f"/knowledge-bases/{identifier}",
                body=request.model_dump(mode="json", exclude_unset=True),
                params={"space_id": space_id},
            ),
        )

    def edit_branch(self, identifier: str, request: BaseEdit, space_id="local") -> KnowledgeBaseVersion:
        return entity(KnowledgeBaseVersion, self.request("PUT", f"/knowledge-bases/{identifier}/branch",
            body=request.model_dump(mode="json", exclude_unset=True), params={"space_id": space_id}))

    def upsert_concept(self, identifier: str, concept_id: str, request: ConceptEdit, space_id="local") -> KnowledgeConcept:
        from urllib.parse import quote
        return entity(KnowledgeConcept, self.request("PUT",
            f"/knowledge-bases/{identifier}/concepts/{quote(concept_id, safe='/')}",
            body=request.model_dump(mode="json", exclude_unset=True), params={"space_id": space_id}))

    def delete_base(
        self, identifier: str, space_id="local", *, permanent=False
    ) -> dict[str, str]:
        return self.request(
            "DELETE",
            f"/knowledge-bases/{identifier}",
            params={"space_id": space_id, "permanent": permanent},
        )

    def bindings(self, space_id="local") -> list[KnowledgeBinding]:
        return [
            entity(KnowledgeBinding, row)
            for row in self.request("GET", "/bindings", params={"space_id": space_id})
        ]

    def bind(
        self, identifier: str, request: BindingAction, source_space_id="local"
    ) -> KnowledgeBinding:
        return entity(
            KnowledgeBinding,
            self.request(
                "POST",
                f"/knowledge-bases/{identifier}/bindings",
                body=request.model_dump(mode="json"),
                params={"source_space_id": source_space_id},
            ),
        )

    def edit_binding(
        self, identifier: str, request: BindingEdit, space_id="local"
    ) -> KnowledgeBinding:
        return entity(
            KnowledgeBinding,
            self.request(
                "PUT",
                f"/knowledge-bases/{identifier}/binding",
                body=request.model_dump(mode="json"),
                params={"space_id": space_id},
            ),
        )

    def versions(self, identifier: str, space_id="local") -> list[KnowledgeBaseVersion]:
        return [
            entity(KnowledgeBaseVersion, row)
            for row in self.request(
                "GET",
                f"/knowledge-bases/{identifier}/versions",
                params={"space_id": space_id},
            )
        ]

    def version(
        self,
        identifier: str,
        operation: Literal["copy", "sync", "publish", "rollback"],
        request: VersionAction,
        space_id="local",
    ) -> KnowledgeBranch:
        return entity(
            KnowledgeBranch,
            self.request(
                "POST",
                f"/knowledge-bases/{identifier}/versions/{operation}",
                body=request.model_dump(mode="json"),
                params={"space_id": space_id},
            ),
        )

    def documents(self, identifier: str, space_id="local") -> list[KnowledgeDocument]:
        return [
            entity(KnowledgeDocument, row)
            for row in self.request(
                "GET",
                f"/knowledge-bases/{identifier}/documents",
                params={"space_id": space_id},
            )
        ]

    def document(self, identifier: str, space_id="local") -> KnowledgeDocument:
        return entity(
            KnowledgeDocument,
            self.request(
                "GET", f"/documents/{identifier}", params={"space_id": space_id}
            ),
        )

    def archive_document(self, identifier: str, space_id="local") -> dict[str, str]:
        return self.request(
            "DELETE", f"/documents/{identifier}", params={"space_id": space_id}
        )

    def job(self, identifier: str, space_id="local") -> KnowledgeIngestJob:
        return entity(
            KnowledgeIngestJob,
            self.request("GET", f"/jobs/{identifier}", params={"space_id": space_id}),
        )

    def jobs(
        self, space_id="local", *, status: str | None = None, limit=20
    ) -> list[KnowledgeIngestJob]:
        params = {"space_id": space_id, "limit": limit}
        if status:
            params["status"] = status
        return [
            entity(KnowledgeIngestJob, row)
            for row in self.request("GET", "/jobs", params=params)
        ]

    def cancel(self, identifier: str, space_id="local") -> KnowledgeIngestJob:
        return entity(
            KnowledgeIngestJob,
            self.request(
                "POST", f"/jobs/{identifier}/cancel", params={"space_id": space_id}
            ),
        )

    def layers(
        self,
        document_id: str,
        layer: Literal["buckets", "chunks", "concepts", "suggestions"],
        space_id="local",
    ) -> list[
        KnowledgeBucket
        | KnowledgeChunk
        | KnowledgeConcept
        | KnowledgeDiscoverySuggestion
    ]:
        model = {
            "buckets": KnowledgeBucket,
            "chunks": KnowledgeChunk,
            "concepts": KnowledgeConcept,
            "suggestions": KnowledgeDiscoverySuggestion,
        }[layer]
        return [
            entity(model, row)
            for row in self.request(
                "GET",
                f"/documents/{document_id}/{layer}",
                params={"space_id": space_id},
            )
        ]

    def edit_layer(
        self,
        layer: Literal["buckets", "chunks", "concepts"],
        identifier: str,
        request: LayerEdit,
        space_id="local",
    ) -> KnowledgeBucket | KnowledgeChunk | KnowledgeConcept:
        model = {
            "buckets": KnowledgeBucket,
            "chunks": KnowledgeChunk,
            "concepts": KnowledgeConcept,
        }[layer]
        return entity(
            model,
            self.request(
                "PUT",
                f"/{layer}/{identifier}",
                body=request.model_dump(mode="json"),
                params={"space_id": space_id},
            ),
        )

    def concepts(self, identifier: str, space_id="local") -> list[KnowledgeConcept]:
        return [
            entity(KnowledgeConcept, row)
            for row in self.request(
                "GET",
                f"/knowledge-bases/{identifier}/concepts",
                params={"space_id": space_id},
            )
        ]

    def suggestions(
        self, identifier: str, space_id="local"
    ) -> list[KnowledgeDiscoverySuggestion]:
        return [
            entity(KnowledgeDiscoverySuggestion, row)
            for row in self.request(
                "GET",
                f"/knowledge-bases/{identifier}/suggestions",
                params={"space_id": space_id},
            )
        ]

    @overload
    def suggestion(self, identifier: str, operation: Literal["review"], space_id="local") -> ReviewResult: ...

    @overload
    def suggestion(self, identifier: str, operation: Literal["reject", "export"], space_id="local") -> KnowledgeDiscoverySuggestion: ...

    @overload
    def suggestion(self, identifier: str, operation: Literal["publish"], space_id="local") -> dict[str, Any]: ...

    def suggestion(
        self,
        identifier: str,
        operation: Literal["review", "reject", "export", "publish"],
        space_id="local",
    ) -> ReviewResult | KnowledgeDiscoverySuggestion | dict[str, Any]:
        value = self.request(
            "POST",
            f"/suggestions/{identifier}/{operation}",
            params={"space_id": space_id},
        )
        return entity(KnowledgeDiscoverySuggestion, value) if operation in {"reject", "export"} else value

    def lint(self, identifier: str, space_id="local") -> dict[str, Any]:
        return self.request(
            "POST",
            f"/knowledge-bases/{identifier}/okf/lint",
            params={"space_id": space_id},
        )

    def import_okf(self, identifier: str, request: Upload) -> OkfImportResult:
        return self.request(
            "POST",
            f"/knowledge-bases/{identifier}/okf",
            body=request.model_dump(mode="json"),
        )

    def export_okf(self, identifier: str, space_id="local") -> bytes:
        response = self.http.get(
            f"/api/km/v1/knowledge-bases/{identifier}/okf",
            params={"space_id": space_id},
        )
        response.raise_for_status()
        return response.content

    def upload(self, knowledge_base_id: str, request: Upload) -> KnowledgeIngestJob:
        return entity(KnowledgeIngestJob, self.request(
            "POST",
            f"/knowledge-bases/{knowledge_base_id}/documents",
            body=request.model_dump(mode="json"),
        ))

    def edit(self, document_id: str, request: Edit, *, space_id: str = "local") -> KnowledgeDocument:
        return entity(KnowledgeDocument, self.request(
            "PUT",
            f"/documents/{document_id}",
            body=request.model_dump(mode="json"),
            params={"space_id": space_id},
        ))

    def query(self, request: QueryRequest) -> KnowledgeSearchResponse:
        return KnowledgeSearchResponse.model_validate(
            self.request("POST", "/query", body=request.model_dump(mode="json"))
        )

    def resolve_citation(self, chunk_id: str, *, space_id: str = "local") -> KnowledgeChunk:
        return entity(KnowledgeChunk, self.request(
            "GET", f"/citations/{chunk_id}", params={"space_id": space_id}
        ))

    def close(self):
        self.http.close()
