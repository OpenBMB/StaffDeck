"""OAuth storage scoped to the authenticated tenant, user and exact MCP resource."""
from __future__ import annotations

import json
from typing import Any

from sqlmodel import Session, select

from app.db.models import MCPOAuthCredential, MCPServer, User, utc_now
from app.security.encryption import decrypt_secret, encrypt_secret


class OAuthOwnershipError(ValueError):
    pass


def _owner(db: Session, tenant_id: str, user_id: str, server_id: str) -> MCPServer:
    user = db.get(User, user_id)
    server = db.get(MCPServer, server_id)
    if not user or not server or user.tenant_id != tenant_id or server.tenant_id != tenant_id:
        raise OAuthOwnershipError("MCP OAuth owner or server is unavailable")
    if not server.enabled or server.transport not in {"streamable_http", "sse"}:
        raise OAuthOwnershipError("MCP OAuth requires an enabled remote server")
    return server


def _row(db: Session, tenant_id: str, user_id: str, server_id: str):
    return db.exec(select(MCPOAuthCredential).where(
        MCPOAuthCredential.tenant_id == tenant_id,
        MCPOAuthCredential.user_id == user_id,
        MCPOAuthCredential.server_id == server_id,
    )).first()


def load_credentials(
    db: Session, tenant_id: str, user_id: str, server_id: str,
) -> dict[str, Any] | None:
    server = _owner(db, tenant_id, user_id, server_id)
    row = _row(db, tenant_id, user_id, server_id)
    if row is None:
        return None
    # Changing a shared server's URL must never forward an old user's bearer token.
    if row.resource_url != server.url:
        raise OAuthOwnershipError("MCP endpoint changed; authorize again")
    return json.loads(decrypt_secret(row.encrypted_payload))


def save_credentials(
    db: Session, tenant_id: str, user_id: str, server_id: str,
    resource_url: str, payload: dict[str, Any],
) -> None:
    server = _owner(db, tenant_id, user_id, server_id)
    if resource_url != server.url:
        raise OAuthOwnershipError("MCP endpoint changed during authorization")
    row = _row(db, tenant_id, user_id, server_id)
    encrypted = encrypt_secret(json.dumps(payload))
    if row is None:
        row = MCPOAuthCredential(
            tenant_id=tenant_id, user_id=user_id, server_id=server_id,
            resource_url=resource_url, encrypted_payload=encrypted,
        )
    else:
        row.resource_url = resource_url
        row.encrypted_payload = encrypted
        row.updated_at = utc_now()
    db.add(row)
    db.commit()


def delete_credentials(db: Session, tenant_id: str, user_id: str, server_id: str) -> None:
    _owner(db, tenant_id, user_id, server_id)
    row = _row(db, tenant_id, user_id, server_id)
    if row:
        db.delete(row)
        db.commit()
