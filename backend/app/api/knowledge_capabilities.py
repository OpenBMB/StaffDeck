"""Optional hosted capabilities. Knowledge storage remains in KM."""

import base64
import os
import logging
from uuid import uuid4
from hmac import compare_digest
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app.db import get_session
from app.db.models import AgentModelBinding, AgentProfile, ModelConfig
from knowledge_core.parser import extract_text
from app.llm import LLMClient
from app.llm.model_config_resolver import resolve_model_config_for_runtime

router = APIRouter(tags=["knowledge-capabilities"])
logger = logging.getLogger(__name__)


class CapabilityRequest(BaseModel):
    tenant_id: str
    space_id: str
    model_id: str | None = None
    prompt: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    filename: str = ""
    content_base64: str = ""
    context: dict[str, Any] = Field(default_factory=dict)


def authorize(body: CapabilityRequest, authorization: str | None):
    token = os.getenv("STAFFDECK_KM_CAPABILITIES_BEARER", "")
    if not token or not compare_digest(authorization or "", "Bearer " + token):
        raise HTTPException(401, "Invalid KM capability credential")
    prefix = f"sd:{body.tenant_id}:"
    if not body.space_id.startswith(prefix):
        raise HTTPException(403, "Space is outside this host namespace")
    return body.space_id.removeprefix(prefix)


@router.post("/api/internal/km/capabilities/model")
def model(body: CapabilityRequest, authorization: str | None = Header(default=None),
          x_km_call_id: str | None = Header(default=None), db: Session = Depends(get_session)):
    employee_id = authorize(body, authorization)
    if employee_id != "gallery":
        employee = db.get(AgentProfile, employee_id)
        if not employee or employee.tenant_id != body.tenant_id:
            raise HTTPException(404, "Host space not found")
    model_id = body.model_id
    if not model_id:
        binding = db.exec(select(AgentModelBinding).where(
            AgentModelBinding.tenant_id == body.tenant_id,
            AgentModelBinding.agent_id == employee_id,
            AgentModelBinding.role == "default",
        )).first()
        model_id = binding.model_config_id if binding else None
    if not model_id:
        row = db.exec(select(ModelConfig).where(ModelConfig.tenant_id == body.tenant_id,
            ModelConfig.is_default == True, ModelConfig.enabled == True)).first()
        model_id = row.id if row else None
    if not model_id:
        raise HTTPException(503, "Host model is not configured")
    config = resolve_model_config_for_runtime(db, body.tenant_id, model_id)
    call_id = x_km_call_id or uuid4().hex
    result = LLMClient(config).generate_json(body.prompt, body.payload)
    logger.warning("KM_MODEL_RESULT call_id=%s config_id=%s provider=%s model=%s result_type=%s result_keys=%s",
                   call_id, config.id, config.api_protocol.value, config.model,
                   type(result).__name__, sorted(result) if isinstance(result, dict) else [])
    logger.warning("KM_MODEL_PHASE call_id=%s phase=%s", call_id,
                   "ingestion" if "sections" in body.payload or "document" in body.payload and "buckets" in body.payload else "query")
    return result


@router.post("/api/internal/km/capabilities/parse")
def parse(body: CapabilityRequest, authorization: str | None = Header(default=None),
          db: Session = Depends(get_session)):
    employee_id = authorize(body, authorization)
    origin = body.context.get("public_origin")
    if origin is not None:
        from app.public_api.auth import principal_for_credential, enforce_agent_access
        from app.public_api.knowledge_pep import enforce_public_knowledge_pep
        from app.public_api.pilotdeck_domain_host import require_pilotdeck_domain_host

        if not isinstance(origin, dict) or origin.get("agent_id") != employee_id:
            raise HTTPException(403, "PUBLIC_HOST_INGEST_IDENTITY_INVALID")
        credential_id = origin.get("credential_id")
        if origin.get("api_job_id"):
            from app.db.models import APIJob
            job = db.get(APIJob, origin["api_job_id"])
            if not job or job.tenant_id != body.tenant_id or job.agent_id != employee_id or job.kind != "knowledge.ingest":
                raise HTTPException(403, "PUBLIC_HOST_INGEST_IDENTITY_INVALID")
            credential_id = job.credential_id
        if not credential_id:
            raise HTTPException(403, "PUBLIC_HOST_INGEST_IDENTITY_INVALID")
        principal = principal_for_credential(db, credential_id)
        if principal.tenant_id != body.tenant_id or principal.actor_user.id != origin.get("actor_user_id") or not principal.can("knowledge:write"):
            raise HTTPException(403, "PUBLIC_HOST_INGEST_IDENTITY_INVALID")
        enforce_agent_access(principal, employee_id, write=True)
        enforce_public_knowledge_pep(db, principal, employee_id, write=True)
        try:
            client = require_pilotdeck_domain_host()
        except RuntimeError as error:
            raise HTTPException(503, "PUBLIC_HOST_FILE_PORT_UNBOUND") from error
        result = client.file_parse(tenant_id=body.tenant_id, actor_user_id=principal.actor_user.id,
                                  agent_id=employee_id, filename=body.filename,
                                  content_base64=body.content_base64, media_type=None)
        return {"text": result["text"], "file_type": result["metadata"]["fileType"]}
    try:
        content = base64.b64decode(body.content_base64, validate=True)
        text, file_type = extract_text(body.filename, content)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"text": text, "file_type": file_type}
