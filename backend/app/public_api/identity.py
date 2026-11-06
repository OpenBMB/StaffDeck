"""Self-service identity and revocation for public API credentials."""
from typing import Annotated

from fastapi import APIRouter, Depends, Response
from sqlmodel import Session

from app.db import get_session
from app.db.models import APICredential, utc_now
from app.public_api.auth import PublicPrincipal, get_public_principal

router = APIRouter(prefix="/auth", tags=["identity"])


@router.get("/me")
def whoami(principal: Annotated[PublicPrincipal, Depends(get_public_principal)]) -> dict:
    return {
        "user_id": principal.actor_user.id,
        "username": principal.actor_user.username,
        "tenant_id": principal.tenant_id,
        "credential_id": principal.credential_id,
        "agent_id": principal.agent_id,
        "scopes": sorted(principal.scopes),
    }


@router.post("/logout", status_code=204)
def logout(
    principal: Annotated[PublicPrincipal, Depends(get_public_principal)],
    db: Annotated[Session, Depends(get_session)],
) -> Response:
    # Only the presented credential is revoked, never another device or key.
    credential = db.get(APICredential, principal.credential_id)
    credential.status = "revoked"
    credential.revoked_at = utc_now()
    credential.updated_at = utc_now()
    db.add(credential)
    db.commit()
    return Response(status_code=204)
