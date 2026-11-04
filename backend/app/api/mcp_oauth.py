"""Authenticated OAuth control endpoints and state-protected provider callback."""
import time
from typing import Annotated
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlmodel import Session

from app.config import get_settings
from app.db import get_session
from app.db.models import MCPServer, User
from app.security.api_policy import module_policy
from app.security.auth import ensure_current_user_tenant, get_current_user
from app.tools.mcp_oauth import OAuthError, complete, prepare
from app.tools.mcp_oauth import disconnect as disconnect_oauth
from app.tools.mcp_oauth_store import (
    OAuthOwnershipError,
    load_credentials,
)

DB = Annotated[Session, Depends(get_session)]
CurrentUser = Annotated[User, Depends(get_current_user)]

router = APIRouter(prefix='/api/enterprise/mcp-oauth', tags=['mcp-oauth'],
                   dependencies=[Depends(module_policy('mcp_server', use_endpoints=('start', 'disconnect')))])
callback_router = APIRouter(prefix='/api/enterprise/mcp-oauth', tags=['mcp-oauth'])


def _server(db: Session, tenant_id: str, server_id: str) -> MCPServer:
    server = db.get(MCPServer, server_id)
    if not server or server.tenant_id != tenant_id or not server.enabled:
        raise HTTPException(404, 'MCP server unavailable')
    return server


@router.post('/{server_id}/start')
def start(server_id: str, tenant_id: str, db: DB,
          current_user: CurrentUser):
    ensure_current_user_tenant(tenant_id, current_user)
    server = _server(db, tenant_id, server_id)
    # Configured origin, never an untrusted request Host or forwarded header.
    callback = get_settings().tool_base_url.rstrip('/') + '/api/enterprise/mcp-oauth/callback?'
    callback += urlencode({'tenant_id': tenant_id, 'user_id': current_user.id, 'server_id': server_id})
    try:
        with httpx.Client(timeout=15, follow_redirects=False) as client:
            url = prepare(db, tenant_id, current_user.id, server_id, server.url or '', callback, client)
        return {'authorization_url': url}
    except (OAuthError, OAuthOwnershipError) as exc:
        raise HTTPException(400, str(exc)) from exc


@callback_router.get('/callback', response_class=HTMLResponse)
def callback(tenant_id: str, user_id: str, server_id: str, db: DB,
             state: str = '', code: str = '', iss: str | None = None):
    server = _server(db, tenant_id, server_id)
    try:
        with httpx.Client(timeout=15, follow_redirects=False) as client:
            complete(db, tenant_id, user_id, server_id, server.url or '', state, code, client, iss)
    except (OAuthError, OAuthOwnershipError) as exc:
        raise HTTPException(400, 'OAuth authorization failed; return to StaffDeck and retry') from exc
    return HTMLResponse('<h1>Authorization completed</h1><p>Return to StaffDeck.</p>',
                        headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})


@router.get('/{server_id}/status')
def status(server_id: str, tenant_id: str, db: DB,
           current_user: CurrentUser):
    ensure_current_user_tenant(tenant_id, current_user)
    _server(db, tenant_id, server_id)
    try:
        data = load_credentials(db, tenant_id, current_user.id, server_id) or {}
        return {'authorized': bool(data.get('tokens')), 'pending': data.get('pending', {}).get('deadline', 0) > time.time()}
    except OAuthOwnershipError:
        return {'authorized': False, 'pending': False}


@router.delete('/{server_id}')
def disconnect(server_id: str, tenant_id: str, db: DB,
               current_user: CurrentUser):
    ensure_current_user_tenant(tenant_id, current_user)
    _server(db, tenant_id, server_id)
    with httpx.Client(timeout=15, follow_redirects=False) as client:
        confirmed = disconnect_oauth(db, tenant_id, current_user.id, server_id, client)
    return {'disconnected': True, 'remote_revocation_confirmed': confirmed}
