from __future__ import annotations

from dataclasses import replace
from time import monotonic
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.db import get_session
from app.db.models import ModelConfig, User, utc_now
from app.llm import LLMClient, LLMError
from app.llm.model_config_resolver import resolve_model_config_for_verification
from app.llm.model_protocols import (
    LEGACY_OPENAI_PROVIDER,
    ModelApiProtocol,
    available_model_protocols,
    current_protocol_options,
    model_config_fingerprint,
    normalize_chat_protocol_options,
    resolve_api_protocol,
    validate_model_base_url,
)
from app.llm.schemas import (
    ModelCapabilityTestResult,
    ModelConfigCreateRequest,
    ModelConfigRead,
    ModelConfigTestResponse,
    ModelConfigUpdateRequest,
)
from app.security.auth import get_current_user, require_current_tenant
from app.security.encryption import decrypt_secret, encrypt_secret, mask_secret
from app.security.permissions import ensure_tenant_admin, require_tenant_admin
from app.security.tenant import ensure_tenant

router = APIRouter(
    prefix="/api/enterprise/model-configs",
    tags=["enterprise:model-configs"],
    dependencies=[Depends(get_current_user)],
)

MODEL_VERIFICATION_DEADLINE_SECONDS = 90.0
MODEL_VERIFICATION_PROBES = (
    ("text", 32, 25.0),
    ("stream", 32, 25.0),
    ("json", 128, 35.0),
)


@router.get(
    "/protocols",
    dependencies=[Depends(require_current_tenant)],
)
def list_model_protocols(tenant_id: str = Query(...)) -> dict[str, list[str]]:
    return {"protocols": available_model_protocols()}


def model_config_read(row: ModelConfig) -> ModelConfigRead:
    api_key = decrypt_secret(row.api_key_encrypted)
    extra_body = row.extra_body_json if isinstance(row.extra_body_json, dict) else {}
    return ModelConfigRead(
        id=row.id,
        tenant_id=row.tenant_id,
        name=row.name,
        provider=row.provider,
        api_protocol=row.api_protocol,
        base_url=row.base_url,
        api_key_masked=mask_secret(api_key),
        model=row.model,
        temperature=row.temperature,
        max_output_tokens=row.max_output_tokens,
        extra_body=dict(extra_body),
        protocol_options=current_protocol_options(
            row.protocol_options_json, ModelApiProtocol(row.api_protocol)
        ),
        legacy_unmapped_options=dict(row.legacy_unmapped_options_json or {}),
        trust_status=row.trust_status,
        verification_attempt_status=row.verification_attempt_status,
        config_revision=row.config_revision,
        security_revision=row.security_revision,
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
    protocol = resolve_api_protocol(request.api_protocol, request.provider)
    if not request.api_key:
        raise HTTPException(status_code=422, detail="MODEL_API_KEY_REQUIRED")
    validate_model_base_url(request.base_url)
    _validate_sampling(protocol, request.temperature, request.max_output_tokens)
    options = _request_protocol_options(request.protocol_options, request.extra_body, protocol)
    row = ModelConfig(
        tenant_id=request.tenant_id,
        name=request.name,
        provider=LEGACY_OPENAI_PROVIDER,
        api_protocol=protocol.value,
        base_url=request.base_url,
        api_key_encrypted=encrypt_secret(request.api_key),
        model=request.model,
        temperature=request.temperature,
        max_output_tokens=request.max_output_tokens,
        extra_body_json=dict(options),
        protocol_options_json={protocol.value: options},
        is_default=False,
        enabled=False,
        trust_status="unverified",
    )
    db.add(row)
    _commit_or_conflict(db)
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
    protocol = resolve_api_protocol(request.api_protocol, request.provider) if (
        request.api_protocol is not None or request.provider is not None
    ) else ModelApiProtocol(row.api_protocol)
    target_temperature = request.temperature if request.temperature is not None else row.temperature
    target_tokens = (
        request.max_output_tokens
        if request.max_output_tokens is not None
        else row.max_output_tokens
    )
    _validate_sampling(protocol, target_temperature, target_tokens)
    if request.base_url is not None:
        validate_model_base_url(request.base_url)
    security_changed = protocol.value != row.api_protocol
    for field in ("base_url", "model"):
        value = getattr(request, field)
        if value is not None and value != getattr(row, field):
            security_changed = True
    if request.api_key not in {None, ""}:
        security_changed = True
    requested_options = None
    if request.protocol_options is not None or request.extra_body is not None:
        requested_options = _request_protocol_options(
            request.protocol_options,
            request.extra_body or {},
            protocol,
        )
        if requested_options != current_protocol_options(row.protocol_options_json, protocol):
            security_changed = True

    for field in ("name", "base_url", "model", "temperature", "max_output_tokens"):
        value = getattr(request, field)
        if value is not None:
            setattr(row, field, value)
    row.api_protocol = protocol.value
    row.provider = LEGACY_OPENAI_PROVIDER
    if request.api_key not in {None, ""}:
        row.api_key_encrypted = encrypt_secret(request.api_key)
        row.key_revision += 1
    if requested_options is not None:
        partitioned = dict(row.protocol_options_json or {})
        partitioned[protocol.value] = requested_options
        row.protocol_options_json = partitioned
        row.extra_body_json = dict(requested_options)
    if request.model_fields_set - {"tenant_id"}:
        row.config_revision += 1
    if security_changed:
        row.security_revision += 1
        row.trust_status = "unverified"
        row.verified_at = None
        row.verified_fingerprint = None
        row.enabled = False
        row.is_default = False
    else:
        if request.enabled is False:
            row.enabled = False
            row.is_default = False
        elif request.enabled is True:
            _require_trusted(row)
            row.enabled = True
        if request.is_default is True:
            _require_trusted(row)
            if not row.enabled:
                raise HTTPException(status_code=409, detail="MODEL_CONFIG_DISABLED")
            _clear_default(db, request.tenant_id)
            row.is_default = True
        elif request.is_default is False:
            row.is_default = False
    row.updated_at = utc_now()
    db.add(row)
    _commit_or_conflict(db)
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
    _require_trusted(row)
    if not row.enabled:
        raise HTTPException(status_code=409, detail="MODEL_CONFIG_DISABLED")
    _clear_default(db, tenant_id)
    row.is_default = True
    row.updated_at = utc_now()
    db.add(row)
    _commit_or_conflict(db)
    db.refresh(row)
    return model_config_read(row)


@router.post(
    "/{config_id}/test",
    response_model=ModelConfigTestResponse,
    dependencies=[Depends(require_tenant_admin)],
)
def test_model_config(
    config_id: str,
    tenant_id: str = Query(...),
    activate_if_initial: bool = False,
    db: Session = Depends(get_session),
) -> ModelConfigTestResponse:
    row = _get_model_config(db, tenant_id, config_id)
    initial_activation_candidate = (
        activate_if_initial
        and row.trust_status == "unverified"
        and not _has_available_model(db, tenant_id)
    )
    attempt_id = uuid4().hex
    started_security_revision = row.security_revision
    row.verification_attempt_id = attempt_id
    row.verification_attempt_status = "verifying"
    row.verification_started_at = utc_now()
    row.verification_attempt_error_code = None
    db.add(row)
    _commit_or_conflict(db)
    capabilities: list[ModelCapabilityTestResult] = []
    output: str | None = None
    verification_started = monotonic()
    try:
        config = resolve_model_config_for_verification(db, tenant_id, config_id, attempt_id)
        for capability_id, max_tokens, probe_timeout in MODEL_VERIFICATION_PROBES:
            remaining = MODEL_VERIFICATION_DEADLINE_SECONDS - (
                monotonic() - verification_started
            )
            if remaining <= 0:
                raise LLMError("MODEL_VERIFICATION_DEADLINE_EXCEEDED")
            probe_config = replace(
                config,
                max_output_tokens=_verification_probe_tokens(
                    config.api_protocol, capability_id, max_tokens
                ),
                timeout_seconds=min(probe_timeout, remaining),
            )
            probe_client = LLMClient(probe_config)
            if capability_id == "text":
                output = probe_client.generate_text(
                    "你是一个连接测试助手。请用一句中文回复连接成功。",
                    {"message": "ping"},
                )
            elif capability_id == "stream":
                stream_text = "".join(
                    probe_client.generate_text_stream(
                        "你是一个连接测试助手。", {"message": "请回复 stream-ok"}
                    )
                )
                if not stream_text.strip():
                    raise LLMError("MODEL_EMPTY_OUTPUT")
            else:
                json_output = probe_client.generate_json(
                    "只返回 JSON object。", {"message": "返回 {\"ok\": true}"}
                )
                if not isinstance(json_output, dict):
                    raise LLMError("MODEL_INVALID_JSON")
            capabilities.append(ModelCapabilityTestResult(id=capability_id, success=True))
        db.refresh(row)
        if (
            row.security_revision != started_security_revision
            or row.verification_attempt_id != attempt_id
            or row.verification_attempt_status != "verifying"
        ):
            return ModelConfigTestResponse(
                success=False,
                message="MODEL_VERIFICATION_STALE",
                output=None,
                attempt_id=attempt_id,
                trust_status=row.trust_status,
                attempt_status=row.verification_attempt_status,
                capabilities=capabilities,
            )
        activated = False
        row.trust_status = "verified"
        row.verified_at = utc_now()
        row.verified_fingerprint = _fingerprint(row)
        row.verification_attempt_status = "succeeded"
        if initial_activation_candidate and not _has_available_model(db, tenant_id):
            row.enabled = True
            row.is_default = True
            activated = True
        db.add(row)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            row = _get_model_config(db, tenant_id, config_id)
            if (
                row.security_revision != started_security_revision
                or row.verification_attempt_id != attempt_id
            ):
                return ModelConfigTestResponse(
                    success=False,
                    message="MODEL_VERIFICATION_STALE",
                    attempt_id=attempt_id,
                    trust_status=row.trust_status,
                    attempt_status=row.verification_attempt_status,
                    capabilities=capabilities,
                )
            row.trust_status = "verified"
            row.verified_at = utc_now()
            row.verified_fingerprint = _fingerprint(row)
            row.verification_attempt_status = "succeeded"
            db.add(row)
            db.commit()
            activated = False
        return ModelConfigTestResponse(
            success=True,
            message="Model connection succeeded",
            activated=activated,
            model=model_config_read(row),
            output=output,
            attempt_id=attempt_id,
            trust_status=row.trust_status,
            attempt_status=row.verification_attempt_status,
            capabilities=capabilities,
        )
    except LLMError as exc:
        db.refresh(row)
        if row.verification_attempt_id == attempt_id:
            row.verification_attempt_status = "failed"
            row.verification_attempt_error_code = _verification_error_code(exc)
            db.add(row)
            db.commit()
        completed_ids = {item.id for item in capabilities}
        failed_id = next(
            (item for item in ("text", "stream", "json") if item not in completed_ids),
            None,
        )
        if failed_id is not None:
            error_code = (
                "MODEL_VERIFICATION_DEADLINE_EXCEEDED"
                if "MODEL_VERIFICATION_DEADLINE_EXCEEDED" in str(exc)
                else _verification_error_code(exc)
            )
            capabilities.append(
                ModelCapabilityTestResult(
                    id=failed_id,
                    success=False,
                    error_code=error_code,
                )
            )
        return ModelConfigTestResponse(
            success=False,
            message=str(exc),
            output=None,
            attempt_id=attempt_id,
            trust_status=row.trust_status,
            attempt_status=row.verification_attempt_status,
            capabilities=capabilities,
        )
    except HTTPException as exc:
        detail = str(exc.detail)
        error_code = detail if detail.startswith("MODEL_") else "MODEL_VERIFICATION_FAILED"
        _mark_verification_failed(db, row, attempt_id, error_code)
        raise
    except Exception:
        _mark_verification_failed(db, row, attempt_id, "MODEL_VERIFICATION_INTERNAL_ERROR")
        raise


def _verification_error_code(exc: Exception) -> str:
    value = str(exc).strip()
    if value.startswith("MODEL_") and " " not in value:
        return value
    return "MODEL_CONNECTION_FAILED"


def _verification_probe_tokens(
    protocol: ModelApiProtocol, capability_id: str, default_tokens: int
) -> int:
    if protocol is ModelApiProtocol.GEMINI_GENERATE_CONTENT and capability_id in {
        "text",
        "stream",
    }:
        return max(default_tokens, 128)
    return default_tokens


def _mark_verification_failed(
    db: Session, row: ModelConfig, attempt_id: str, error_code: str
) -> None:
    db.refresh(row)
    if row.verification_attempt_id != attempt_id:
        return
    row.verification_attempt_status = "failed"
    row.verification_attempt_error_code = error_code
    db.add(row)
    db.commit()


def _get_model_config(db: Session, tenant_id: str, config_id: str) -> ModelConfig:
    ensure_tenant(db, tenant_id)
    row = db.get(ModelConfig, config_id)
    if not row or row.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Model config not found")
    return row


def _has_available_model(db: Session, tenant_id: str) -> bool:
    return (
        db.exec(
            select(ModelConfig).where(
                ModelConfig.tenant_id == tenant_id,
                (ModelConfig.enabled == True) | (ModelConfig.is_default == True),  # noqa: E712
            )
        ).first()
        is not None
    )


def _clear_default(db: Session, tenant_id: str) -> None:
    rows = db.exec(select(ModelConfig).where(ModelConfig.tenant_id == tenant_id)).all()
    for row in rows:
        row.is_default = False
        row.updated_at = utc_now()
        db.add(row)


def _request_protocol_options(
    protocol_options: dict | None,
    extra_body: dict,
    protocol: ModelApiProtocol,
) -> dict:
    if protocol_options is not None and extra_body and protocol_options != extra_body:
        raise HTTPException(status_code=422, detail="MODEL_PROTOCOL_OPTIONS_CONFLICT")
    if protocol in {
        ModelApiProtocol.ANTHROPIC_MESSAGES,
        ModelApiProtocol.GEMINI_GENERATE_CONTENT,
    }:
        if (protocol_options is not None and protocol_options != {}) or extra_body:
            raise HTTPException(status_code=422, detail="MODEL_PROTOCOL_OPTIONS_INVALID")
        return {}
    if protocol_options is not None:
        return normalize_chat_protocol_options(protocol_options)
    return normalize_chat_protocol_options(extra_body)


def _validate_sampling(
    protocol: ModelApiProtocol, temperature: float, max_output_tokens: int
) -> None:
    max_temperature = (
        1 if protocol is ModelApiProtocol.ANTHROPIC_MESSAGES else 2
    )
    if not 0 <= temperature <= max_temperature:
        raise HTTPException(status_code=422, detail="MODEL_TEMPERATURE_INVALID")
    if max_output_tokens <= 0:
        raise HTTPException(status_code=422, detail="MODEL_MAX_OUTPUT_TOKENS_INVALID")


def _require_trusted(row: ModelConfig) -> None:
    if row.trust_status == "legacy_trusted" and row.api_protocol == "openai_chat_completions":
        return
    if row.trust_status != "verified" or row.verified_fingerprint != _fingerprint(row):
        raise HTTPException(status_code=409, detail="MODEL_CONFIG_VERIFICATION_REQUIRED")


def _fingerprint(row: ModelConfig) -> str:
    protocol = ModelApiProtocol(row.api_protocol)
    return model_config_fingerprint(
        api_protocol=row.api_protocol,
        base_url=row.base_url,
        model=row.model,
        key_revision=row.key_revision,
        protocol_options=current_protocol_options(row.protocol_options_json, protocol),
        security_revision=row.security_revision,
    )


def _commit_or_conflict(db: Session) -> None:
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="MODEL_DEFAULT_CONFLICT") from exc
