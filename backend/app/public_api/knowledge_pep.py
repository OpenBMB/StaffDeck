"""Explicit PEP for public Knowledge routes that call native functions directly.

FastAPI dependencies on enterprise routes do not run during direct Python calls.
The selected staff, resource branch, actor and tenant are checked here before
public reads, writes or deferred ingest admission.
"""
from __future__ import annotations

from sqlmodel import Session

from app.api import knowledge_bases as native_bases
from app.public_api.auth import PublicPrincipal, enforce_agent_access
from app.public_api.errors import PublicAPIError
from app.public_api.sessions import ensure_public_agent
from app.security.permissions import ensure_agent_scope_manager, require_agent_scope_viewer


def enforce_public_knowledge_pep(
    db: Session,
    principal: PublicPrincipal,
    agent_id: str,
    *,
    write: bool = False,
    knowledge_base_id: str | None = None,
    document_id: str | None = None,
) -> None:
    db.info["staffdeck_actor_id"] = principal.actor_user.id
    db.info["staffdeck_km_agent_id"] = agent_id
    enforce_agent_access(principal, agent_id, write=write)
    ensure_public_agent(db, principal, agent_id)
    if write:
        ensure_agent_scope_manager(db, principal.tenant_id, agent_id, principal.actor_user)
    else:
        require_agent_scope_viewer(principal.tenant_id, agent_id, principal.actor_user, db)
    if knowledge_base_id:
        # Checks the visible branch/version, not only tenant ownership of the row.
        native_bases.get_knowledge_base(knowledge_base_id, principal.tenant_id, agent_id, db)
    if document_id:
        from app.knowledge.http_adapter import StaffDeckKnowledgeAdapter
        row = StaffDeckKnowledgeAdapter(
            db, principal.tenant_id, agent_id, principal.actor_user,
        ).call("GET", f"/documents/{document_id}")
        if row["knowledge_base_id"] != knowledge_base_id:
            raise PublicAPIError(404, "DOCUMENT_NOT_FOUND", "Document not found in this knowledge base.")


def reject_public_scope_override(body: dict, *fields: str) -> None:
    if any(field in body for field in fields):
        raise PublicAPIError(400, "PUBLIC_SCOPE_OVERRIDE", "Scope is determined by the public path and principal.")
