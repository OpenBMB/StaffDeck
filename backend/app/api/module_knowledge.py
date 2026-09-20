"""Module Protocol facade over StaffDeck's existing knowledge owner APIs."""

from __future__ import annotations

import base64
import os
from typing import Any, Literal

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError
from sqlmodel import Session

from app.api.knowledge import (
    cancel_job,
    chunk_read,
    confirm_discovery,
    get_bucket_chunks,
    get_document,
    get_document_buckets,
    get_job,
    import_okf_bundle,
    list_discoveries,
    list_jobs,
    list_documents,
    reject_discovery,
    search_knowledge,
    update_bucket,
    update_chunk,
    update_document,
    upload_document,
)
from app.api.knowledge_bases import (
    create_knowledge_base,
    delete_knowledge_base,
    export_okf,
    get_knowledge_base,
    get_okf_concept,
    lint_okf,
    list_knowledge_base_versions,
    list_knowledge_bases,
    list_okf_concepts,
    promote_knowledge_base_to_overall,
    rollback_knowledge_base,
    sync_knowledge_base_from_overall,
    update_knowledge_base,
    upsert_okf_concept,
)
from app.db import get_session
from app.db.models import KnowledgeChunk, User
from app.knowledge.schema import (
    KnowledgeBaseCreateRequest,
    KnowledgeBaseRollbackRequest,
    KnowledgeBaseUpdateRequest,
    KnowledgeBucketUpdateRequest,
    KnowledgeChunkUpdateRequest,
    KnowledgeConceptUpdateRequest,
    KnowledgeDocumentUpdateRequest,
    KnowledgeDocumentUploadRequest,
    KnowledgeOkfImportRequest,
    KnowledgeSearchRequest,
)

PROTOCOL_VERSION = "2.0"
IMPLEMENTATION_ID = "staffdeck.knowledge"
CONTRACT = "staffdeck.knowledge/v1"
TRANSPORT = "module-http-v2"
METHODS = (
    "list_bases",
    "create_base",
    "get_base",
    "update_base",
    "delete_base",
    "list_versions",
    "sync_base",
    "publish_version",
    "rollback_version",
    "list_documents",
    "get_document",
    "import_document",
    "import_okf",
    "update_document",
    "delete_document",
    "list_document_buckets",
    "update_bucket",
    "list_bucket_chunks",
    "update_chunk",
    "get_job",
    "list_jobs",
    "cancel_job",
    "list_okf_concepts",
    "get_okf_concept",
    "upsert_okf_concept",
    "export_okf",
    "lint_okf",
    "list_discoveries",
    "confirm_discovery",
    "reject_discovery",
    "query",
    "resolve_citation",
)

router = APIRouter(tags=["module:knowledge"])


class ModuleCallEnvelope(BaseModel):
    kind: Literal["request"]
    method: Literal["module_call"]
    messageId: str
    runId: str
    operationId: str
    requestId: str
    idempotencyKey: str | None = None
    module: Literal["knowledge"]
    payload: dict[str, Any]


@router.get("/module-manifest")
def module_manifest() -> dict[str, Any]:
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "implementationId": IMPLEMENTATION_ID,
        "contract": CONTRACT,
        "transport": TRANSPORT,
        "methods": list(METHODS),
        "state": {"ownership": "module", "scope": "tenant", "persistence": "staffdeck-database"},
    }


@router.post("/v2/module/call")
def module_call(
    request_body: Any = Body(...),  # noqa: B008 - FastAPI request body declaration.
    db: Session = Depends(get_session),  # noqa: B008 - FastAPI dependency declaration.
) -> JSONResponse:
    try:
        request = ModuleCallEnvelope.model_validate(request_body)
    except ValidationError as error:
        body = request_body if isinstance(request_body, dict) else {}
        return _failure_ids(
            str(body.get("messageId") or "unknown"),
            str(body.get("requestId") or "unknown"),
            "MODULE_PROTOCOL_INCOMPATIBLE",
            "Knowledge module received an invalid request envelope.",
            400,
            {"validation": error.errors()},
        )
    operation = request.payload.get("operation")
    raw_input = request.payload.get("input")
    if (
        not isinstance(operation, str)
        or operation not in METHODS
        or not isinstance(raw_input, dict)
    ):
        return _failure(
            request,
            "MODULE_PROTOCOL_INCOMPATIBLE",
            "Knowledge call requires a supported operation and object input.",
            400,
        )
    try:
        result = _dispatch(operation, raw_input, db)
    except HTTPException as error:
        return _failure(
            request,
            f"STAFFDECK_HTTP_{error.status_code}",
            str(error.detail),
            error.status_code,
            {"statusCode": error.status_code, "detail": error.detail},
        )
    except (KeyError, TypeError, ValueError) as error:
        return _failure(request, "KNOWLEDGE_INPUT_INVALID", str(error), 422)
    return JSONResponse(
        {
            "kind": "response",
            "messageId": f"response-{request.messageId}",
            "inReplyTo": request.messageId,
            "requestId": request.requestId,
            "ok": True,
            "payload": {"result": _json_value(result)},
        }
    )


def _dispatch(operation: str, value: dict[str, Any], db: Session) -> Any:
    tenant_id = _text(value, "tenantId", "tenant_id", default_env="STAFFDECK_KNOWLEDGE_TENANT_ID")
    agent_id = _optional_text(value, "agentId", "agent_id")

    if operation == "list_bases":
        return list_knowledge_bases(tenant_id, agent_id, db)
    if operation == "create_base":
        user = _actor(db, value, tenant_id)
        request = KnowledgeBaseCreateRequest(
            tenant_id=tenant_id,
            name=_text(value, "name"),
            description=_optional_text(value, "description"),
            capability_scope=value.get("capabilityScope", value.get("capability_scope", "general")),
            metadata=_object(value.get("metadata")),
        )
        return create_knowledge_base(request, agent_id, db, user)
    if operation == "get_base":
        return get_knowledge_base(
            _text(value, "knowledgeBaseId", "knowledge_base_id"), tenant_id, agent_id, db
        )
    if operation == "update_base":
        request = KnowledgeBaseUpdateRequest(
            tenant_id=tenant_id,
            name=_optional_text(value, "name"),
            description=_optional_text(value, "description"),
            status=value.get("status"),
            capability_scope=value.get("capabilityScope", value.get("capability_scope")),
            metadata=value.get("metadata") if isinstance(value.get("metadata"), dict) else None,
        )
        return update_knowledge_base(
            _text(value, "knowledgeBaseId", "knowledge_base_id"), request, agent_id, db,
            _actor(db, value, tenant_id),
        )
    if operation == "delete_base":
        return delete_knowledge_base(
            _text(value, "knowledgeBaseId", "knowledge_base_id"),
            tenant_id,
            agent_id,
            db,
            _actor(db, value, tenant_id),
        )
    if operation == "list_versions":
        return list_knowledge_base_versions(
            _text(value, "knowledgeBaseId", "knowledge_base_id"), tenant_id, agent_id, db
        )
    if operation == "sync_base":
        return sync_knowledge_base_from_overall(
            _text(value, "knowledgeBaseId", "knowledge_base_id"), tenant_id,
            _text(value, "agentId", "agent_id"), db, _actor(db, value, tenant_id),
        )
    if operation == "publish_version":
        if not agent_id:
            raise ValueError("publish_version requires agentId for StaffDeck branch promotion.")
        return promote_knowledge_base_to_overall(
            _text(value, "knowledgeBaseId", "knowledge_base_id"),
            tenant_id,
            agent_id,
            db,
            _actor(db, value, tenant_id),
        )
    if operation == "rollback_version":
        return rollback_knowledge_base(
            _text(value, "knowledgeBaseId", "knowledge_base_id"),
            KnowledgeBaseRollbackRequest(
                tenant_id=tenant_id,
                agent_id=_text(value, "agentId", "agent_id"),
                version=_text(value, "version"),
            ),
            db,
            _actor(db, value, tenant_id),
        )
    if operation == "list_documents":
        return list_documents(
            tenant_id,
            _optional_text(value, "knowledgeBaseId", "knowledge_base_id"),
            agent_id,
            bool(value.get("includeAllVersions", value.get("include_all_versions", False))),
            db,
        )
    if operation == "get_document":
        return get_document(_text(value, "documentId", "document_id"), tenant_id, agent_id, db)
    if operation == "import_document":
        request = KnowledgeDocumentUploadRequest(
            tenant_id=tenant_id,
            knowledge_base_id=_optional_text(value, "knowledgeBaseId", "knowledge_base_id"),
            filename=_text(value, "filename"),
            content_base64=_text(value, "contentBase64", "content_base64"),
            title=_optional_text(value, "title"),
            capability_scope=value.get("capabilityScope", value.get("capability_scope", "general")),
            metadata=_object(value.get("metadata")),
        )
        return upload_document(request, agent_id, db, _actor(db, value, tenant_id))
    if operation == "import_okf":
        return import_okf_bundle(
            KnowledgeOkfImportRequest(
                tenant_id=tenant_id,
                knowledge_base_id=_optional_text(value, "knowledgeBaseId", "knowledge_base_id"),
                filename=_text(value, "filename"),
                content_base64=_text(value, "contentBase64", "content_base64"),
                agent_id=agent_id,
            ),
            db,
            _actor(db, value, tenant_id),
        )
    if operation in {"update_document", "delete_document"}:
        request = KnowledgeDocumentUpdateRequest(
            tenant_id=tenant_id,
            title=_optional_text(value, "title"),
            status="archived" if operation == "delete_document" else value.get("status"),
            metadata=value.get("metadata") if isinstance(value.get("metadata"), dict) else None,
            content_md=value.get("contentMd", value.get("content_md")),
            expected_updated_at=value.get("expectedUpdatedAt", value.get("expected_updated_at")),
        )
        return update_document(
            _text(value, "documentId", "document_id"),
            request,
            db,
            _actor(db, value, tenant_id),
            agent_id,
        )
    if operation == "list_document_buckets":
        return get_document_buckets(
            _text(value, "documentId", "document_id"), tenant_id, agent_id, db
        )
    if operation == "update_bucket":
        return update_bucket(
            _text(value, "bucketId", "bucket_id"),
            KnowledgeBucketUpdateRequest(
                tenant_id=tenant_id,
                title=_optional_text(value, "title"),
                summary=_optional_text(value, "summary"),
                metadata=value.get("metadata") if isinstance(value.get("metadata"), dict) else None,
            ),
            db,
            _actor(db, value, tenant_id),
        )
    if operation == "list_bucket_chunks":
        return get_bucket_chunks(_text(value, "bucketId", "bucket_id"), tenant_id, agent_id, db)
    if operation == "update_chunk":
        return update_chunk(
            _text(value, "chunkId", "chunk_id"),
            KnowledgeChunkUpdateRequest(
                tenant_id=tenant_id,
                content=_optional_text(value, "content"),
                summary=_optional_text(value, "summary"),
                metadata=value.get("metadata") if isinstance(value.get("metadata"), dict) else None,
            ),
            db,
            _actor(db, value, tenant_id),
        )
    if operation == "get_job":
        return get_job(_text(value, "jobId", "job_id"), tenant_id, agent_id, db)
    if operation == "list_jobs":
        return list_jobs(
            tenant_id,
            agent_id,
            _optional_text(value, "status"),
            _integer(value, "limit", default=8),
            db,
        )
    if operation == "cancel_job":
        return cancel_job(
            _text(value, "jobId", "job_id"), tenant_id, db, _actor(db, value, tenant_id)
        )
    if operation == "list_okf_concepts":
        return list_okf_concepts(
            _text(value, "knowledgeBaseId", "knowledge_base_id"), tenant_id, agent_id,
            _optional_text(value, "conceptType", "concept_type"), db,
        )
    if operation == "get_okf_concept":
        return get_okf_concept(
            _text(value, "knowledgeBaseId", "knowledge_base_id"),
            _text(value, "conceptId", "concept_id"), tenant_id, agent_id, db,
        )
    if operation == "upsert_okf_concept":
        return upsert_okf_concept(
            _text(value, "knowledgeBaseId", "knowledge_base_id"),
            _text(value, "conceptId", "concept_id"),
            KnowledgeConceptUpdateRequest(
                tenant_id=tenant_id,
                content_md=_text(value, "contentMd", "content_md"),
                document_id=_optional_text(value, "documentId", "document_id"),
                status=value.get("status", "active"),
            ),
            agent_id,
            db,
            _actor(db, value, tenant_id),
        )
    if operation == "export_okf":
        response = export_okf(
            _text(value, "knowledgeBaseId", "knowledge_base_id"), tenant_id, agent_id, db
        )
        return {
            "content_base64": base64.b64encode(response.body).decode("ascii"),
            "media_type": response.media_type,
            "filename": _content_disposition_filename(response.headers.get("content-disposition")),
        }
    if operation == "lint_okf":
        return lint_okf(
            _text(value, "knowledgeBaseId", "knowledge_base_id"), tenant_id, agent_id, db
        )
    if operation == "list_discoveries":
        return list_discoveries(
            tenant_id,
            _optional_text(value, "knowledgeBaseId", "knowledge_base_id"),
            _optional_text(value, "status"),
            agent_id,
            db,
        )
    if operation == "confirm_discovery":
        return confirm_discovery(
            _text(value, "suggestionId", "suggestion_id"), tenant_id, db,
            _actor(db, value, tenant_id),
        )
    if operation == "reject_discovery":
        return reject_discovery(
            _text(value, "suggestionId", "suggestion_id"), tenant_id, db,
            _actor(db, value, tenant_id),
        )
    if operation == "query":
        request = KnowledgeSearchRequest(
            tenant_id=tenant_id,
            agent_id=agent_id,
            query=_text(value, "query"),
            query_type=value.get("queryType", value.get("query_type", "answer")),
            desired_evidence=value.get("desiredEvidence", value.get("desired_evidence")),
            scope=_object(value.get("scope")),
            model_config_id=value.get("modelConfigId", value.get("model_config_id")),
            mode=value.get("mode", "chat"),
            knowledge_base_ids=_string_list(
                value.get("knowledgeBaseIds", value.get("knowledge_base_ids", []))
            ),
            knowledge_base_version_ids=_string_list(
                value.get("knowledgeBaseVersionIds", value.get("knowledge_base_version_ids", []))
            ),
            document_ids=_string_list(value.get("documentIds", value.get("document_ids", []))),
            max_bucket_rounds=value.get("maxBucketRounds", value.get("max_bucket_rounds", 2)),
            max_buckets=value.get("maxBuckets", value.get("max_buckets", 4)),
            max_chunks=value.get("maxChunks", value.get("max_chunks", 8)),
            budget_tokens=value.get("budgetTokens", value.get("budget_tokens", 4000)),
            max_depth=value.get("maxDepth", value.get("max_depth", 2)),
            need_evidence_pack=value.get("needEvidencePack", value.get("need_evidence_pack", True)),
        )
        return search_knowledge(request, db, _actor(db, value, tenant_id))
    if operation == "resolve_citation":
        chunk_id = _text(value, "chunkId", "chunk_id")
        chunk = db.get(KnowledgeChunk, chunk_id)
        if not chunk or chunk.tenant_id != tenant_id:
            raise HTTPException(status_code=404, detail="Knowledge citation not found")
        return chunk_read(chunk)
    raise ValueError(f"Unsupported knowledge operation: {operation}")


def _actor(db: Session, value: dict[str, Any], tenant_id: str) -> User:
    user_id = _optional_text(value, "actorUserId", "actor_user_id") or os.getenv(
        "STAFFDECK_KNOWLEDGE_USER_ID"
    )
    if not user_id:
        raise ValueError("Knowledge operation requires actorUserId or STAFFDECK_KNOWLEDGE_USER_ID.")
    user = db.get(User, user_id)
    if not user or user.tenant_id != tenant_id:
        raise HTTPException(status_code=401, detail="Knowledge module actor not found")
    db.info["staffdeck_actor_id"] = user.id
    return user


def _text(value: dict[str, Any], *keys: str, default_env: str | None = None) -> str:
    result = _optional_text(value, *keys)
    if not result and default_env:
        result = os.getenv(default_env, "").strip() or None
    if not result:
        raise ValueError(f"Missing required field: {keys[0]}")
    return result


def _optional_text(value: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        item = value.get(key)
        if isinstance(item, str) and item.strip():
            return item.strip()
    return None


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("Expected a list of strings.")
    return value


def _integer(value: dict[str, Any], key: str, *, default: int) -> int:
    result = value.get(key, default)
    if isinstance(result, bool) or not isinstance(result, int):
        raise ValueError(f"Expected integer field: {key}")
    return result


def _content_disposition_filename(value: str | None) -> str | None:
    if not value:
        return None
    marker = 'filename="'
    start = value.find(marker)
    if start < 0:
        return None
    end = value.find('"', start + len(marker))
    return value[start + len(marker):] if end < 0 else value[start + len(marker):end]


def _json_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    return value


def _failure(
    request: ModuleCallEnvelope,
    code: str,
    message: str,
    status_code: int,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    return _failure_ids(request.messageId, request.requestId, code, message, status_code, details)


def _failure_ids(
    message_id: str,
    request_id: str,
    code: str,
    message: str,
    status_code: int,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    return JSONResponse(
        {
            "kind": "response",
            "messageId": f"response-{message_id}",
            "inReplyTo": message_id,
            "requestId": request_id,
            "ok": False,
            "code": code,
            "error": {
                "code": code,
                "message": message,
                "retryability": "unsafe",
                "details": details or {},
            },
        },
        status_code=status_code,
    )
