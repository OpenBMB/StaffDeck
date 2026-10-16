from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.agents.branching import visible_knowledge_base_versions
from app.capabilities.contracts import (
    CapabilityContext,
    CitationDetail,
    KnowledgeHit,
    KnowledgeRuntime,
    KnowledgeScope,
    KnowledgeSearchQuery,
    KnowledgeSearchResult,
)
from app.capabilities.errors import CapabilityErrorInfo, CapabilityProviderError
from app.db.models import new_id
from app.knowledge.schema import KnowledgeSearchRequest, KnowledgeSearchResponse


class LocalKnowledgeRuntime(KnowledgeRuntime):
    """Adapter for the existing local service; it has no AgentLoop dependency."""

    provider_id = "local_knowledge"

    def __init__(
        self,
        service_factory: Callable[[Any], Any],
        db: Any,
        model_config: Any | None = None,
    ) -> None:
        self._service_factory = service_factory
        self._db = db
        self._model_config = model_config

    def list_scopes(self, context: CapabilityContext) -> tuple[KnowledgeScope, ...]:
        from app.knowledge.http_adapter import enabled
        if enabled():
            self._db.info["staffdeck_actor_id"] = context.user_id
        versions = visible_knowledge_base_versions(
            self._db, context.tenant_id, context.agent_id
        )
        return tuple(
            KnowledgeScope(
                scope_id=knowledge_base_id,
                name=version.name,
                version=version.version,
                metadata=dict(version.metadata_json or {}),
            )
            for knowledge_base_id, version in versions.items()
        )

    def search(
        self, context: CapabilityContext, request: KnowledgeSearchQuery
    ) -> KnowledgeSearchResult:
        allowed_query_types = {"answer", "policy_check", "tool_discovery", "skill_discovery"}
        if request.query_type not in allowed_query_types:
            raise CapabilityProviderError(
                CapabilityErrorInfo(
                    code="KNOWLEDGE_UNSUPPORTED_QUERY_TYPE",
                    message=f"unsupported Knowledge query type: {request.query_type}",
                    retryable=False,
                    request_id=context.request_id,
                    provider_id=self.provider_id,
                )
            )
        legacy_request = KnowledgeSearchRequest(
            tenant_id=context.tenant_id,
            agent_id=context.agent_id,
            query=request.query,
            query_type=request.query_type,
            scope=dict(request.scope),
            max_chunks=request.max_chunks,
            budget_tokens=request.budget_tokens,
            mode="chat",
        )
        from app.knowledge.http_adapter import StaffDeckKnowledgeAdapter
        self._db.info["staffdeck_actor_id"] = context.user_id
        response = StaffDeckKnowledgeAdapter(
            self._db, context.tenant_id, context.agent_id,
        ).handle("search_knowledge", {"request": legacy_request})
        if not isinstance(response, KnowledgeSearchResponse):
            raise TypeError("local Knowledge provider returned an invalid response")
        items = tuple(
            KnowledgeHit(
                hit_id=str(chunk.id),
                content=chunk.content,
                source_ref=chunk.source_ref,
                metadata=dict(chunk.metadata or {}),
            )
            for chunk in response.chunks
        )
        return KnowledgeSearchResult(
            query_id=new_id("kquery"),
            items=items,
            extensions={
                "local_knowledge": {
                    "request_id": context.request_id,
                    "trace": response.trace,
                    "route_trace": response.route_trace,
                    "selected_documents": response.selected_documents,
                    "selected_concepts": response.selected_concepts,
                    "evidence_pack": response.evidence_pack,
                }
            },
        )

    def resolve_citation(
        self, context: CapabilityContext, provider_citation_ref: str
    ) -> CitationDetail:
        from app.knowledge.http_adapter import StaffDeckKnowledgeAdapter, enabled
        if enabled():
            self._db.info["staffdeck_actor_id"] = context.user_id
            row = StaffDeckKnowledgeAdapter(self._db, context.tenant_id, context.agent_id).call(
                "GET", f"/citations/{provider_citation_ref}",
            )
            return CitationDetail(citation_id=row["id"], title=None,
                                  content=row["content"], source_ref=row.get("source_ref"))
        raise CapabilityProviderError(
            CapabilityErrorInfo(
                code="KNOWLEDGE_CITATION_NOT_DURABLE",
                message="Local Knowledge citation must be resolved from a persisted snapshot",
                retryable=False,
                request_id=context.request_id,
                provider_id=self.provider_id,
            )
        )
