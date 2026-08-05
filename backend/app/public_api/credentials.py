from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlmodel import Session, select

from app.db import get_session
from app.db.models import APIClient, APICredential, AgentProfile, utc_now
from app.public_api.auth import (
    PublicPrincipal,
    generate_api_key,
    get_public_or_admin_principal,
)
from app.public_api.credential_profiles import AGENT_KEY_ALLOWED_SCOPES
from app.public_api.errors import PublicAPIError
from app.public_api.schemas import (
    APIClientCreate,
    APIClientRead,
    APIClientUpdate,
    APICredentialCreate,
    APICredentialCreated,
    APICredentialRead,
)


router = APIRouter(tags=["credentials"])

def _scope_allowed(granted: set[str], requested: str) -> bool:
    return (
        "*" in granted
        or requested in granted
        or f"{requested.split(':', 1)[0]}:*" in granted
    )


def _require_credentials_write(principal: PublicPrincipal) -> None:
    if not principal.bootstrap_user and not principal.can("credentials:write"):
        raise PublicAPIError(403, "INSUFFICIENT_SCOPE", "credentials:write is required.")
    if not principal.bootstrap_user and principal.agent_id:
        raise PublicAPIError(403, "TENANT_KEY_REQUIRED", "A tenant management key is required.")


def _client_read(row: APIClient) -> APIClientRead:
    return APIClientRead(
        id=row.id,
        name=row.name,
        description=row.description,
        scopes=list(row.scopes_json or []),
        status=row.status,
        metadata=dict(row.metadata_json or {}),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _credential_read(row: APICredential) -> APICredentialRead:
    return APICredentialRead(
        id=row.id,
        client_id=row.client_id,
        agent_id=row.agent_id,
        name=row.name,
        key_prefix=f"{row.key_prefix}…",
        scopes=list(row.scopes_json or []),
        status=row.status,
        expires_at=row.expires_at,
        last_used_at=row.last_used_at,
        created_at=row.created_at,
        revoked_at=row.revoked_at,
    )


@router.get("/api-clients", response_model=list[APIClientRead])
def list_api_clients(
    principal: PublicPrincipal = Depends(get_public_or_admin_principal),
    db: Session = Depends(get_session),
) -> list[APIClientRead]:
    _require_credentials_write(principal)
    rows = db.exec(
        select(APIClient)
        .where(APIClient.tenant_id == principal.tenant_id)
        .order_by(APIClient.created_at.desc())
    ).all()
    return [_client_read(row) for row in rows]


@router.post("/api-clients", response_model=APIClientRead, status_code=201)
def create_api_client(
    request: APIClientCreate,
    principal: PublicPrincipal = Depends(get_public_or_admin_principal),
    db: Session = Depends(get_session),
) -> APIClientRead:
    _require_credentials_write(principal)
    existing = db.exec(
        select(APIClient).where(
            APIClient.tenant_id == principal.tenant_id,
            APIClient.name == request.name.strip(),
        )
    ).first()
    if existing:
        raise PublicAPIError(409, "API_CLIENT_EXISTS", "An API client with this name exists.")
    row = APIClient(
        tenant_id=principal.tenant_id,
        name=request.name.strip(),
        description=request.description,
        scopes_json=sorted(set(request.scopes)),
        created_by_user_id=principal.actor_user.id,
        metadata_json=request.metadata,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _client_read(row)


@router.patch("/api-clients/{client_id}", response_model=APIClientRead)
def update_api_client(
    client_id: str,
    request: APIClientUpdate,
    principal: PublicPrincipal = Depends(get_public_or_admin_principal),
    db: Session = Depends(get_session),
) -> APIClientRead:
    _require_credentials_write(principal)
    row = db.get(APIClient, client_id)
    if not row or row.tenant_id != principal.tenant_id:
        raise PublicAPIError(404, "API_CLIENT_NOT_FOUND", "API client not found.")
    values = request.model_dump(exclude_unset=True)
    if "name" in values:
        row.name = str(values["name"]).strip()
    if "description" in values:
        row.description = values["description"]
    if "scopes" in values:
        row.scopes_json = sorted(set(values["scopes"] or []))
    if "status" in values:
        row.status = values["status"]
    if "metadata" in values:
        row.metadata_json = values["metadata"] or {}
    row.updated_at = utc_now()
    db.add(row)
    db.commit()
    db.refresh(row)
    return _client_read(row)


@router.get("/api-clients/{client_id}/credentials", response_model=list[APICredentialRead])
def list_api_credentials(
    client_id: str,
    principal: PublicPrincipal = Depends(get_public_or_admin_principal),
    db: Session = Depends(get_session),
) -> list[APICredentialRead]:
    _require_credentials_write(principal)
    client = db.get(APIClient, client_id)
    if not client or client.tenant_id != principal.tenant_id:
        raise PublicAPIError(404, "API_CLIENT_NOT_FOUND", "API client not found.")
    rows = db.exec(
        select(APICredential)
        .where(APICredential.client_id == client_id)
        .order_by(APICredential.created_at.desc())
    ).all()
    return [_credential_read(row) for row in rows]


@router.post(
    "/api-clients/{client_id}/credentials",
    response_model=APICredentialCreated,
    status_code=201,
)
def create_api_credential(
    client_id: str,
    request: APICredentialCreate,
    principal: PublicPrincipal = Depends(get_public_or_admin_principal),
    db: Session = Depends(get_session),
) -> APICredentialCreated:
    _require_credentials_write(principal)
    client = db.get(APIClient, client_id)
    if not client or client.tenant_id != principal.tenant_id:
        raise PublicAPIError(404, "API_CLIENT_NOT_FOUND", "API client not found.")
    scopes = set(request.scopes)
    client_scopes = set(client.scopes_json or [])
    if not all(_scope_allowed(client_scopes, scope) for scope in scopes):
        raise PublicAPIError(400, "SCOPE_ESCALATION", "Credential scopes exceed client scopes.")
    if request.agent_id:
        agent = db.get(AgentProfile, request.agent_id)
        if not agent or agent.tenant_id != principal.tenant_id:
            raise PublicAPIError(404, "AGENT_NOT_FOUND", "Agent not found.")
        if not scopes.issubset(AGENT_KEY_ALLOWED_SCOPES):
            raise PublicAPIError(400, "AGENT_SCOPE_INVALID", "Agent key contains management scopes.")
    token, prefix, digest = generate_api_key()
    row = APICredential(
        tenant_id=principal.tenant_id,
        client_id=client.id,
        agent_id=request.agent_id,
        name=request.name.strip(),
        key_prefix=prefix,
        key_digest=digest,
        scopes_json=sorted(scopes),
        expires_at=request.expires_at,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    payload = _credential_read(row).model_dump()
    return APICredentialCreated(**payload, api_key=token)


@router.post("/credentials/{credential_id}:rotate", response_model=APICredentialCreated)
def rotate_api_credential(
    credential_id: str,
    principal: PublicPrincipal = Depends(get_public_or_admin_principal),
    db: Session = Depends(get_session),
) -> APICredentialCreated:
    _require_credentials_write(principal)
    row = db.get(APICredential, credential_id)
    if not row or row.tenant_id != principal.tenant_id:
        raise PublicAPIError(404, "API_CREDENTIAL_NOT_FOUND", "API credential not found.")
    token, prefix, digest = generate_api_key()
    row.key_prefix = prefix
    row.key_digest = digest
    row.status = "active"
    row.revoked_at = None
    row.updated_at = utc_now()
    db.add(row)
    db.commit()
    db.refresh(row)
    return APICredentialCreated(**_credential_read(row).model_dump(), api_key=token)


@router.post("/credentials/{credential_id}:revoke", response_model=APICredentialRead)
def revoke_api_credential(
    credential_id: str,
    principal: PublicPrincipal = Depends(get_public_or_admin_principal),
    db: Session = Depends(get_session),
) -> APICredentialRead:
    _require_credentials_write(principal)
    row = db.get(APICredential, credential_id)
    if not row or row.tenant_id != principal.tenant_id:
        raise PublicAPIError(404, "API_CREDENTIAL_NOT_FOUND", "API credential not found.")
    row.status = "revoked"
    row.revoked_at = utc_now()
    row.updated_at = utc_now()
    db.add(row)
    db.commit()
    db.refresh(row)
    return _credential_read(row)
