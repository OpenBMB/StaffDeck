from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session, select

from app.db import get_session
from app.db.models import ModelConfig, User, utc_now
from app.llm import LLMClient, LLMError
from app.llm.schemas import ModelConfigCreateRequest, ModelConfigRead, ModelConfigTestResponse, ModelConfigUpdateRequest
from app.security.auth import get_current_user, require_current_tenant
from app.security.encryption import decrypt_secret, encrypt_secret, mask_secret
from app.security.permissions import ensure_tenant_admin, require_tenant_admin
from app.security.tenant import ensure_tenant

router = APIRouter(
    prefix="/api/enterprise/model-configs",
    tags=["enterprise:model-configs"],
    dependencies=[Depends(get_current_user)],
)


def model_config_read(row: ModelConfig) -> ModelConfigRead:
    api_key = decrypt_secret(row.api_key_encrypted)
    extra_body = row.extra_body_json if isinstance(row.extra_body_json, dict) else {}
    return ModelConfigRead(
        id=row.id,
        tenant_id=row.tenant_id,
        name=row.name,
        provider=row.provider,
        base_url=row.base_url,
        api_key_masked=mask_secret(api_key),
        model=row.model,
        temperature=row.temperature,
        max_output_tokens=row.max_output_tokens,
        extra_body=dict(extra_body),
        is_default=row.is_default,
        enabled=row.enabled,
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


@router.get("", response_model=list[ModelConfigRead], dependencies=[Depends(require_current_tenant)])
def list_model_configs(
    tenant_id: str = Query(...), db: Session = Depends(get_session)
) -> list[ModelConfigRead]:
    ensure_tenant(db, tenant_id)
    rows = db.exec(select(ModelConfig).where(ModelConfig.tenant_id == tenant_id)).all()
    return [model_config_read(row) for row in rows]


@router.post("", response_model=ModelConfigRead)
def create_model_config(
    request: ModelConfigCreateRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> ModelConfigRead:
    ensure_tenant_admin(request.tenant_id, current_user)
    ensure_tenant(db, request.tenant_id)
    existing_count = len(db.exec(select(ModelConfig).where(ModelConfig.tenant_id == request.tenant_id)).all())
    is_default = request.is_default or existing_count == 0
    if is_default:
        _clear_default(db, request.tenant_id)
    row = ModelConfig(
        tenant_id=request.tenant_id,
        name=request.name,
        provider=request.provider,
        base_url=request.base_url,
        api_key_encrypted=encrypt_secret(request.api_key),
        model=request.model,
        temperature=request.temperature,
        max_output_tokens=request.max_output_tokens,
        extra_body_json=dict(request.extra_body),
        is_default=is_default,
        enabled=request.enabled,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return model_config_read(row)


@router.put("/{config_id}", response_model=ModelConfigRead)
def update_model_config(
    config_id: str,
    request: ModelConfigUpdateRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> ModelConfigRead:
    ensure_tenant_admin(request.tenant_id, current_user)
    row = _get_model_config(db, request.tenant_id, config_id)
    if request.is_default:
        _clear_default(db, request.tenant_id)
    for field in ("name", "provider", "base_url", "model", "temperature", "max_output_tokens", "enabled"):
        value = getattr(request, field)
        if value is not None:
            setattr(row, field, value)
    if request.api_key is not None:
        row.api_key_encrypted = encrypt_secret(request.api_key)
    if request.extra_body is not None:
        row.extra_body_json = dict(request.extra_body)
    if request.is_default is not None:
        row.is_default = request.is_default
    row.updated_at = utc_now()
    db.add(row)
    db.commit()
    db.refresh(row)
    return model_config_read(row)


@router.post(
    "/{config_id}/set-default",
    response_model=ModelConfigRead,
    dependencies=[Depends(require_tenant_admin)],
)
def set_default_model_config(
    config_id: str, tenant_id: str = Query(...), db: Session = Depends(get_session)
) -> ModelConfigRead:
    row = _get_model_config(db, tenant_id, config_id)
    _clear_default(db, tenant_id)
    row.is_default = True
    row.updated_at = utc_now()
    db.add(row)
    db.commit()
    db.refresh(row)
    return model_config_read(row)


@router.post(
    "/{config_id}/test",
    response_model=ModelConfigTestResponse,
    dependencies=[Depends(require_tenant_admin)],
)
def test_model_config(
    config_id: str, tenant_id: str = Query(...), db: Session = Depends(get_session)
) -> ModelConfigTestResponse:
    row = _get_model_config(db, tenant_id, config_id)
    try:
        output = LLMClient(row).generate_text(
            "你是一个连接测试助手。请用一句中文回复连接成功。",
            {"message": "ping"},
        )
        return ModelConfigTestResponse(success=True, message="Model connection succeeded", output=output)
    except LLMError as exc:
        return ModelConfigTestResponse(success=False, message=str(exc), output=None)


def _get_model_config(db: Session, tenant_id: str, config_id: str) -> ModelConfig:
    ensure_tenant(db, tenant_id)
    row = db.get(ModelConfig, config_id)
    if not row or row.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Model config not found")
    return row


def _clear_default(db: Session, tenant_id: str) -> None:
    rows = db.exec(select(ModelConfig).where(ModelConfig.tenant_id == tenant_id)).all()
    for row in rows:
        row.is_default = False
        row.updated_at = utc_now()
        db.add(row)
