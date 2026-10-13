"""Authenticated management facade for a selected PD project agent."""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlmodel import Session

from app.api import module_knowledge as native
from app.db import get_session
from app.db.models import KnowledgeBucket, KnowledgeChunk, KnowledgeDocument, KnowledgeDiscoverySuggestion, KnowledgeIngestJob
from app.public_api.auth import PublicPrincipal, require_scopes
from app.public_api.errors import PublicAPIError
from app.public_api.knowledge_pep import enforce_public_knowledge_pep

router = APIRouter(tags=["pilotdeck-project-knowledge"])
READ = {"list_bases", "get_base", "list_versions", "list_documents", "get_document",
        "list_document_buckets", "list_bucket_chunks", "get_job", "list_jobs",
        "list_okf_concepts", "get_okf_concept", "export_okf", "list_discoveries", "query", "resolve_citation"}


@router.post("/agents/{agent_id}/knowledge-management/v2/module/call")
def call(agent_id: str, body: native.ModuleCallEnvelope,
         principal: PublicPrincipal = Depends(require_scopes("knowledge:read")),
         db: Session = Depends(get_session)) -> JSONResponse:
    operation = body.payload.get("operation")
    raw = body.payload.get("input")
    if operation not in native.METHODS or not isinstance(raw, dict):
        return native._failure(body, "MODULE_PROTOCOL_INCOMPATIBLE", "Unsupported knowledge operation.", 400)
    try:
        write = operation not in READ
        if write and not principal.can("knowledge:write"):
            raise PublicAPIError(403, "SCOPE_FORBIDDEN", "Knowledge write scope is required.")
        for names, expected in ((('tenantId', 'tenant_id'), principal.tenant_id),
                                (('actorUserId', 'actor_user_id'), principal.actor_user.id),
                                (('agentId', 'agent_id'), agent_id)):
            if any(raw.get(name) not in (None, expected) for name in names):
                raise PublicAPIError(403, "PROJECT_BINDING_SCOPE_MISMATCH", "Knowledge belongs to another project.")
        base = raw.get('knowledgeBaseId', raw.get('knowledge_base_id'))
        # Native direct calls bypass route dependencies: validate every resource owner here.
        resources = ((KnowledgeDocument, ('documentId', 'document_id')),
                     (KnowledgeBucket, ('bucketId', 'bucket_id')),
                     (KnowledgeChunk, ('chunkId', 'chunk_id')),
                     (KnowledgeDiscoverySuggestion, ('suggestionId', 'suggestion_id')),
                     (KnowledgeIngestJob, ('jobId', 'job_id')))
        for model, names in resources:
            identifier = next((raw.get(name) for name in names if raw.get(name)), None)
            if identifier:
                row = db.get(model, identifier)
                if row is None or row.tenant_id != principal.tenant_id:
                    raise PublicAPIError(404, "RESOURCE_NOT_FOUND", "Knowledge resource is unavailable.")
                resource_base = row.knowledge_base_id
                enforce_public_knowledge_pep(db, principal, agent_id, write=write, knowledge_base_id=resource_base)
        enforce_public_knowledge_pep(db, principal, agent_id, write=write, knowledge_base_id=base)
        for identifier in raw.get('knowledgeBaseIds', raw.get('knowledge_base_ids', [])) or []:
            enforce_public_knowledge_pep(db, principal, agent_id, knowledge_base_id=identifier)
        value = {**raw, 'tenantId': principal.tenant_id, 'tenant_id': principal.tenant_id,
                 'agentId': agent_id, 'agent_id': agent_id,
                 'actorUserId': principal.actor_user.id, 'actor_user_id': principal.actor_user.id}
        if operation == 'query':
            from app.knowledge.public_host_selection import use_public_host_retrieval
            with use_public_host_retrieval():
                result = native._dispatch(operation, value, db)
        else:
            result = native._dispatch(operation, value, db)
        return JSONResponse({'kind': 'response', 'messageId': f'response-{body.messageId}',
                             'inReplyTo': body.messageId, 'requestId': body.requestId, 'ok': True,
                             'payload': {'result': native._json_value(result)}})
    except PublicAPIError as exc:
        return native._failure(body, exc.code, exc.detail, exc.status_code)
    except HTTPException as exc:
        return native._failure(body, f'STAFFDECK_HTTP_{exc.status_code}', str(exc.detail), exc.status_code)
    except (KeyError, ValueError, TypeError) as exc:
        return native._failure(body, 'KNOWLEDGE_INPUT_INVALID', str(exc), 422)
