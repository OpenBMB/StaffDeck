from fastapi import HTTPException
from sqlmodel import Session

from app.db.models import Tenant
from app.object_cache import cached_model, store_model

# 租户只在 seed 时创建、无运行时写端点：缓存 TTL 可以放宽到 60s
TENANT_CACHE_TTL_SECONDS = 60


def ensure_tenant(session: Session, tenant_id: str) -> Tenant:
    cache_key = f"auth:tenant:{tenant_id}"
    tenant = cached_model(cache_key, Tenant, ttl_seconds=TENANT_CACHE_TTL_SECONDS)
    if tenant is None:
        tenant = session.get(Tenant, tenant_id)
        if not tenant:
            raise HTTPException(status_code=404, detail=f"Tenant not found: {tenant_id}")
        store_model(cache_key, tenant, ttl_seconds=TENANT_CACHE_TTL_SECONDS)
    return tenant

