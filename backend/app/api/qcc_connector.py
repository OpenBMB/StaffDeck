"""QCC unified MCP preset; authentication stays in the generic OAuth layer."""
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app.api.tools import create_mcp_server
from app.db import get_session
from app.db.models import MCPServer, User
from app.security.api_policy import resource_ref
from app.security.auth import ensure_current_user_tenant, get_current_user
from app.security.module_policy import require_resource
from app.security.permissions import ensure_open_gallery_admin
from app.tools.tool_schema import MCPServerConnection, MCPServerCreateRequest

QCC_RESOURCE = 'https://agent.qcc.com/mcp/company/stream'
QCC_SERVER_NAME = 'qcc_company'
DB = Annotated[Session, Depends(get_session)]
CurrentUser = Annotated[User, Depends(get_current_user)]
router = APIRouter(prefix='/api/enterprise/mcp-presets/qcc', tags=['mcp-presets'])


def _existing(db: Session, tenant_id: str) -> MCPServer | None:
    row = db.exec(select(MCPServer).where(
        MCPServer.tenant_id == tenant_id, MCPServer.name == QCC_SERVER_NAME,
    )).first()
    if row and (row.url != QCC_RESOURCE or row.transport != 'streamable_http'
                or row.headers_json):
        raise HTTPException(409, 'qcc_company already exists with a different configuration; no changes made')
    return row


@router.get('')
def get_qcc(tenant_id: str, db: DB, current_user: CurrentUser):
    ensure_current_user_tenant(tenant_id, current_user)
    row = _existing(db, tenant_id)
    if row:
        ref = resource_ref(db, tenant_id, 'mcp_server', row.id)
        require_resource(current_user, ref, 'view', module='mcp_server', local_checked=True)
    # Do not return shared server headers or anyone's credentials.
    return {'server_id': row.id if row else None, 'enabled': bool(row and row.enabled)}


@router.post('')
def install_qcc(tenant_id: str, db: DB, current_user: CurrentUser):
    ensure_current_user_tenant(tenant_id, current_user)
    ensure_open_gallery_admin(tenant_id, current_user)
    existing = _existing(db, tenant_id)
    if existing:
        return {'server_id': existing.id, 'enabled': existing.enabled}
    server = create_mcp_server(MCPServerCreateRequest(
        tenant_id=tenant_id, name=QCC_SERVER_NAME, display_name='企查查 MCP',
        description='OAuth 统一入口；按账号权限发现企业及历史数据工具，通过能力搜索按需加载。',
        bucket='企查查', connection=MCPServerConnection(
            transport='streamable_http', url=QCC_RESOURCE, headers={},
        ),
    ), db, current_user)
    return {'server_id': server.id, 'enabled': server.enabled}
