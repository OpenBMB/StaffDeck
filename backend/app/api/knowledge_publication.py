"""StaffDeck owns validation and creation of resources suggested by KM."""

from __future__ import annotations

import json
import os
from hmac import compare_digest
from typing import Any
from types import SimpleNamespace

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.db import get_session
from app.db.models import KnowledgePublicationReceipt
from app.knowledge.publication_adapter import StaffDeckSuggestionPublisher


router = APIRouter(tags=["knowledge-publication"])


class PublicationRequest(BaseModel):
    suggestion: dict[str, Any]


@router.post("/api/internal/km/publications")
def publish(
    body: PublicationRequest,
    authorization: str | None = Header(default=None),
    idempotency_key: str | None = Header(default=None),
    db: Session = Depends(get_session),
):
    token = os.getenv("STAFFDECK_KM_PUBLISHER_BEARER", "")
    if not token or not compare_digest(authorization or "", "Bearer " + token):
        raise HTTPException(401, "Invalid KM publisher credential")
    value = body.suggestion
    tenant_id, suggestion_id = value.get("tenant_id"), value.get("id")
    if not tenant_id or not suggestion_id or idempotency_key != f"km:{tenant_id}:{suggestion_id}":
        raise HTTPException(422, "Invalid publication identity")
    existing = db.exec(select(KnowledgePublicationReceipt).where(
        KnowledgePublicationReceipt.idempotency_key == idempotency_key,
    )).first()
    if existing:
        return existing.target_reference_json
    suggestion_type = value.get("suggestion_type")
    payload = value.get("payload_json") or {}
    if suggestion_type == "procedure":
        steps = payload.get("steps")
        if not isinstance(steps, list) or not steps:
            raise HTTPException(422, "Procedure suggestion contains no steps")
        nodes = [{"node_id": f"step_{index}", "type": "response",
                  "name": f"Step {index + 1}",
                  "instruction": step if isinstance(step, str) else json.dumps(step, ensure_ascii=False),
                  "allowed_actions": ["answer_user"]}
                 for index, step in enumerate(steps)]
        payload = {"draft_skill": {
            "skill_id": "km." + suggestion_id, "version": "1.0.0",
            "name": value.get("title") or suggestion_id,
            "business_domain": "knowledge", "description": payload.get("description") or "",
            "nodes": nodes,
            "edges": [{"source_node_id": nodes[index]["node_id"],
                       "next_node_id": nodes[index + 1]["node_id"]}
                      for index in range(len(nodes) - 1)],
            "start_node_id": nodes[0]["node_id"], "terminal_node_ids": [nodes[-1]["node_id"]],
        }}
        suggestion_type = "skill"
    elif suggestion_type == "interface":
        payload = {**payload, "name": payload.get("name") or "km." + suggestion_id}
        suggestion_type = "tool"
    suggestion = SimpleNamespace(
        id=suggestion_id, tenant_id=tenant_id,
        knowledge_base_id=value.get("knowledge_base_id", ""),
        document_id=value.get("document_id", ""), suggestion_type=suggestion_type,
        title=value.get("title") or suggestion_id, reason=value.get("reason"),
    )
    try:
        # Claim and resource creation share one host transaction. A losing
        # concurrent request cannot create a second resource.
        receipt = KnowledgePublicationReceipt(
            id="km:" + suggestion_id, tenant_id=tenant_id, suggestion_id=suggestion_id,
            idempotency_key=idempotency_key,
        )
        db.add(receipt)
        db.flush()
        service = StaffDeckSuggestionPublisher(db)
        if suggestion_type == "skill":
            reference = service._confirm_skill(suggestion, payload)
        elif suggestion_type == "tool":
            reference = service._confirm_tool(suggestion, payload)
        else:
            raise HTTPException(422, "Suggestion cannot be published as a StaffDeck resource")
        receipt.target_reference_json = reference
        db.add(receipt)
        db.commit()
        return reference
    except IntegrityError:
        db.rollback()
        receipt = db.exec(select(KnowledgePublicationReceipt).where(
            KnowledgePublicationReceipt.idempotency_key == idempotency_key,
        )).first()
        if receipt:
            return receipt.target_reference_json
        raise HTTPException(409, "Publication resource conflict")
    except ValueError as exc:
        db.rollback()
        raise HTTPException(422, str(exc)) from exc
