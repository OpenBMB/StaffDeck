from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from time import monotonic
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.codex_subscription import (
    CodexSubscriptionAccount,
    CodexSubscriptionError,
    get_codex_subscription_service,
)
from app.db import get_session
from app.db.models import AgentModelBinding, ModelConfig, User, utc_now
from app.llm import LLMClient, LLMError
from app.llm.model_config_resolver import (
    ResolvedModelConfig,
    resolve_model_config_for_verification,
    snapshot_model_config,
)
from app.llm.model_protocols import (
    LEGACY_OPENAI_PROVIDER,
    ModelApiProtocol,
    ModelAuthMode,
    available_model_protocols,
    current_protocol_options,
    model_config_fingerprint,
    normalize_chat_protocol_options,
    resolve_api_protocol,
    resolve_auth_mode,
    validate_model_base_url,
)
from app.llm.schemas import (
    CodexSubscriptionAccountRead,
    ModelCapabilityTestResult,
    ModelConfigCreateRequest,
    ModelConfigRead,
    ModelConfigTestResponse,
    ModelConfigUpdateRequest,
    ModelListModelsRequest,
    ModelListModelsResponse,
    ModelOption,
    ModelProviderErrorDetail,
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
    ("text", 25.0),
    ("stream", 25.0),
    ("json", 35.0),
)


@router.get(
    "/protocols",
    dependencies=[Depends(require_current_tenant)],
)
def list_model_protocols(tenant_id: str = Query(...)) -> dict[str, list[str]]:
    return {"protocols": available_model_protocols()}


@router.post("/list-models", response_model=ModelListModelsResponse)
def list_provider_models(
    request: ModelListModelsRequest,
    current_user: User = Depends(get_current_user),
) -> ModelListModelsResponse:
    """按渠道自身的接口拉取可用模型，供新建模型向导自动填充；不落库、不需要已存在的模型配置。"""
    ensure_tenant_admin(request.tenant_id, current_user)
    protocol = resolve_api_protocol(request.api_protocol, None)
    is_subscription = protocol is ModelApiProtocol.CODEX_APP_SERVER
    if is_subscription:
        base_url = None
        api_key_encrypted = ""
        auth_mode = ModelAuthMode.CHATGPT_SUBSCRIPTION
    else:
        if not request.api_key:
            raise HTTPException(status_code=422, detail="MODEL_API_KEY_REQUIRED")
        validate_model_base_url(request.base_url)
        base_url = request.base_url
        api_key_encrypted = encrypt_secret(request.api_key)
        auth_mode = ModelAuthMode.API_KEY
    config = ResolvedModelConfig(
        id="",
        tenant_id=request.tenant_id,
        api_protocol=protocol,
        base_url=base_url,
        api_key_encrypted=api_key_encrypted,
        model="",
        temperature=0.2,
        max_output_tokens=1,
        protocol_options={},
        legacy_extra_body={},
        config_revision=0,
        security_revision=0,
        purpose="verification",
        auth_mode=auth_mode,
        timeout_seconds=15.0,
    )
    try:
        options = LLMClient(config).list_models()
    except LLMError as exc:
        return ModelListModelsResponse(
            success=False,
            models=[],
            error=ModelProviderErrorDetail.model_validate(exc.public_detail()),
        )
    return ModelListModelsResponse(
        success=True,
        models=[ModelOption(id=item["id"], label=item["label"]) for item in options],
    )


def model_config_read(row: ModelConfig) -> ModelConfigRead:
    auth_mode = resolve_auth_mode(getattr(row, "auth_mode", None))
    api_key = (
        decrypt_secret(row.api_key_encrypted)
        if auth_mode is ModelAuthMode.API_KEY and row.api_key_encrypted
        else ""
    )
    extra_body = row.extra_body_json if isinstance(row.extra_body_json, dict) else {}
    return ModelConfigRead(
        id=row.id,
        tenant_id=row.tenant_id,
        name=row.name,
        provider=row.provider,
        auth_mode=auth_mode.value,
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


@router.get(
    "", response_model=list[ModelConfigRead], dependencies=[Depends(require_current_tenant)]
)
def list_model_configs(
    tenant_id: str = Query(...), db: Session = Depends(get_session)
) -> list[ModelConfigRead]:
    ensure_tenant(db, tenant_id)
    rows = db.exec(select(ModelConfig).where(ModelConfig.tenant_id == tenant_id)).all()
    return [model_config_read(row) for row in rows]


@router.get(
    "/codex-subscription/account",
    response_model=CodexSubscriptionAccountRead,
    dependencies=[Depends(require_current_tenant)],
)
def get_codex_subscription_account(
    tenant_id: str = Query(...), current_user: User = Depends(get_current_user)
) -> CodexSubscriptionAccountRead:
    ensure_tenant_admin(tenant_id, current_user)
    return _subscription_account_response(get_codex_subscription_service().account_status)


@router.post(
    "/codex-subscription/login",
    response_model=CodexSubscriptionAccountRead,
    dependencies=[Depends(require_current_tenant)],
)
def start_codex_subscription_login(
    tenant_id: str = Query(...), current_user: User = Depends(get_current_user)
) -> CodexSubscriptionAccountRead:
    ensure_tenant_admin(tenant_id, current_user)
    return _subscription_account_response(get_codex_subscription_service().start_login)


@router.post(
    "/codex-subscription/login/cancel",
    response_model=CodexSubscriptionAccountRead,
    dependencies=[Depends(require_current_tenant)],
)
def cancel_codex_subscription_login(
    tenant_id: str = Query(...), current_user: User = Depends(get_current_user)
) -> CodexSubscriptionAccountRead:
    ensure_tenant_admin(tenant_id, current_user)
    return _subscription_account_response(get_codex_subscription_service().cancel_login)


@router.post(
    "/codex-subscription/logout",
    response_model=CodexSubscriptionAccountRead,
    dependencies=[Depends(require_current_tenant)],
)
def logout_codex_subscription(
    tenant_id: str = Query(...), current_user: User = Depends(get_current_user)
) -> CodexSubscriptionAccountRead:
    ensure_tenant_admin(tenant_id, current_user)
    return _subscription_account_response(get_codex_subscription_service().logout)


@router.post("", response_model=ModelConfigRead)
def create_model_config(
    request: ModelConfigCreateRequest,
    verify_before_save: bool = False,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> ModelConfigRead:
    ensure_tenant_admin(request.tenant_id, current_user)
    ensure_tenant(db, request.tenant_id)
    auth_mode = resolve_auth_mode(request.auth_mode)
    if auth_mode is ModelAuthMode.CHATGPT_SUBSCRIPTION:
        _validate_subscription_request(request)
        protocol = ModelApiProtocol.CODEX_APP_SERVER
        base_url = None
        api_key_encrypted = ""
        options = {}
        extra_body = {}
    else:
        protocol = resolve_api_protocol(request.api_protocol, request.provider)
        if not request.api_key:
            raise HTTPException(status_code=422, detail="MODEL_API_KEY_REQUIRED")
        validate_model_base_url(request.base_url)
        base_url = request.base_url
        api_key_encrypted = encrypt_secret(request.api_key)
        options = _request_protocol_options(request.protocol_options, protocol)
        extra_body = _request_extra_body(request.extra_body, protocol)
    _validate_sampling(protocol, request.temperature, request.max_output_tokens)
    row = ModelConfig(
        tenant_id=request.tenant_id,
        name=request.name,
        provider=LEGACY_OPENAI_PROVIDER,
        auth_mode=auth_mode.value,
        api_protocol=protocol.value,
        base_url=base_url,
        api_key_encrypted=api_key_encrypted,
        model=request.model,
        temperature=request.temperature,
        max_output_tokens=request.max_output_tokens,
        extra_body_json=extra_body,
        protocol_options_json={protocol.value: options},
        is_default=False,
        enabled=False,
        trust_status="unverified",
    )
    if verify_before_save and request.enabled:
        _verify_candidate_for_save(row)
        row.enabled = True
        if request.is_default or not _has_available_model(db, request.tenant_id):
            _clear_default(db, request.tenant_id)
            row.is_default = True
    db.add(row)
    _commit_or_conflict(db)
    db.refresh(row)
    return model_config_read(row)


@router.put("/{config_id}", response_model=ModelConfigRead)
def update_model_config(
    config_id: str,
    request: ModelConfigUpdateRequest,
    verify_before_save: bool = False,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> ModelConfigRead:
    ensure_tenant_admin(request.tenant_id, current_user)
    row = _get_model_config(db, request.tenant_id, config_id)
    has_other_available_model = _has_available_model(
        db, request.tenant_id, exclude_config_id=config_id
    )
    current_auth_mode = resolve_auth_mode(getattr(row, "auth_mode", None))
    auth_mode = resolve_auth_mode(request.auth_mode or current_auth_mode)
    if auth_mode is ModelAuthMode.CHATGPT_SUBSCRIPTION:
        _validate_subscription_update_request(request)
        protocol = ModelApiProtocol.CODEX_APP_SERVER
    else:
        if (
            current_auth_mode is ModelAuthMode.CHATGPT_SUBSCRIPTION
            and request.auth_mode is not None
            and not request.api_key
        ):
            raise HTTPException(status_code=422, detail="MODEL_API_KEY_REQUIRED")
        protocol = (
            resolve_api_protocol(request.api_protocol, request.provider)
            if (request.api_protocol is not None or request.provider is not None)
            else ModelApiProtocol(row.api_protocol)
        )
    target_temperature = request.temperature if request.temperature is not None else row.temperature
    target_tokens = (
        request.max_output_tokens
        if request.max_output_tokens is not None
        else row.max_output_tokens
    )
    _validate_sampling(protocol, target_temperature, target_tokens)
    if auth_mode is ModelAuthMode.API_KEY and request.base_url is not None:
        validate_model_base_url(request.base_url)
    security_changed = (
        auth_mode is not current_auth_mode or protocol.value != row.api_protocol
    )
    for field in ("base_url", "model"):
        value = getattr(request, field)
        if value is not None and value != getattr(row, field):
            security_changed = True
    if auth_mode is ModelAuthMode.API_KEY and request.api_key not in {None, ""}:
        security_changed = True
    requested_options = None
    if auth_mode is ModelAuthMode.API_KEY and request.protocol_options is not None:
        requested_options = _request_protocol_options(request.protocol_options, protocol)
        if requested_options != current_protocol_options(row.protocol_options_json, protocol):
            security_changed = True
    requested_extra_body = None
    if auth_mode is ModelAuthMode.API_KEY and request.extra_body is not None:
        requested_extra_body = _request_extra_body(request.extra_body, protocol)
        if requested_extra_body != dict(row.extra_body_json or {}):
            security_changed = True

    for field in ("name", "model", "temperature", "max_output_tokens"):
        value = getattr(request, field)
        if value is not None:
            setattr(row, field, value)
    if auth_mode is ModelAuthMode.API_KEY and request.base_url is not None:
        row.base_url = request.base_url
    row.auth_mode = auth_mode.value
    row.api_protocol = protocol.value
    row.provider = LEGACY_OPENAI_PROVIDER
    if auth_mode is ModelAuthMode.CHATGPT_SUBSCRIPTION:
        if row.api_key_encrypted:
            row.key_revision += 1
        row.api_key_encrypted = ""
        row.base_url = None
        row.protocol_options_json = {}
        row.extra_body_json = {}
    elif request.api_key not in {None, ""}:
        row.api_key_encrypted = encrypt_secret(request.api_key)
        row.key_revision += 1
    if requested_options is not None:
        partitioned = dict(row.protocol_options_json or {})
        partitioned[protocol.value] = requested_options
        row.protocol_options_json = partitioned
    if requested_extra_body is not None:
        row.extra_body_json = requested_extra_body
    if request.model_fields_set - {"tenant_id"}:
        row.config_revision += 1
    if security_changed:
        row.security_revision += 1
        row.trust_status = "unverified"
        row.verified_at = None
        row.verified_fingerprint = None
        row.enabled = False
        row.is_default = False
    if verify_before_save and request.enabled is True:
        try:
            _verify_candidate_for_save(row)
        except Exception:
            db.rollback()
            raise
        row.enabled = True
        if request.is_default is True or not has_other_available_model:
            _clear_default(db, request.tenant_id)
            row.is_default = True
        elif request.is_default is False:
            row.is_default = False
    elif not security_changed:
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


@router.delete("/{config_id}")
def delete_model_config(
    config_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> dict[str, str]:
    ensure_tenant_admin(tenant_id, current_user)
    row = _get_model_config(db, tenant_id, config_id)
    bindings = db.exec(
        select(AgentModelBinding).where(
            AgentModelBinding.tenant_id == tenant_id,
            AgentModelBinding.model_config_id == config_id,
        )
    ).all()
    for binding in bindings:
        db.delete(binding)
    db.delete(row)
    db.commit()
    return {"status": "deleted"}


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
    try:
        config = resolve_model_config_for_verification(db, tenant_id, config_id, attempt_id)
        capabilities, output = _run_verification_probes(config)
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
            message=exc.code or "MODEL_CONNECTION_FAILED",
            output=None,
            attempt_id=attempt_id,
            trust_status=row.trust_status,
            attempt_status=row.verification_attempt_status,
            capabilities=capabilities,
            error=ModelProviderErrorDetail.model_validate(exc.public_detail()),
        )
    except HTTPException as exc:
        detail = str(exc.detail)
        error_code = detail if detail.startswith("MODEL_") else "MODEL_VERIFICATION_FAILED"
        _mark_verification_failed(db, row, attempt_id, error_code)
        raise
    except Exception:
        _mark_verification_failed(db, row, attempt_id, "MODEL_VERIFICATION_INTERNAL_ERROR")
        raise


def _verify_candidate_for_save(row: ModelConfig) -> None:
    attempt_id = uuid4().hex
    row.verification_attempt_id = attempt_id
    row.verification_attempt_status = "verifying"
    row.verification_started_at = utc_now()
    row.verification_attempt_error_code = None
    config = replace(snapshot_model_config(row), purpose="verification")
    try:
        _run_verification_probes(config)
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=exc.public_detail()) from exc
    row.trust_status = "verified"
    row.verified_at = utc_now()
    row.verified_fingerprint = _fingerprint(row)
    row.verification_attempt_status = "succeeded"


def _run_verification_probes(
    config: ResolvedModelConfig,
) -> tuple[list[ModelCapabilityTestResult], str | None]:
    capabilities: list[ModelCapabilityTestResult] = []
    output: str | None = None
    verification_started = monotonic()
    for capability_id, probe_timeout in MODEL_VERIFICATION_PROBES:
        remaining = MODEL_VERIFICATION_DEADLINE_SECONDS - (monotonic() - verification_started)
        if remaining <= 0:
            raise LLMError("MODEL_VERIFICATION_DEADLINE_EXCEEDED")
        probe_config = replace(
            config,
            timeout_seconds=min(probe_timeout, remaining),
            # Verification is a real model call too. Keep Max Tokens governed
            # by the saved model configuration instead of an invisible probe
            # budget.
            max_output_tokens=config.max_output_tokens,
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
                "只返回 JSON object。", {"message": '返回 {"ok": true}'}
            )
            if not isinstance(json_output, dict):
                raise LLMError("MODEL_INVALID_JSON")
        capabilities.append(ModelCapabilityTestResult(id=capability_id, success=True))
    return capabilities, output


def _verification_error_code(exc: Exception) -> str:
    if isinstance(exc, LLMError) and exc.code:
        return exc.code
    value = str(exc).strip()
    if value.startswith("MODEL_") and " " not in value:
        return value
    return "MODEL_CONNECTION_FAILED"


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


def _has_available_model(
    db: Session, tenant_id: str, *, exclude_config_id: str | None = None
) -> bool:
    statement = select(ModelConfig).where(
        ModelConfig.tenant_id == tenant_id,
        (ModelConfig.enabled == True) | (ModelConfig.is_default == True),  # noqa: E712
    )
    if exclude_config_id:
        statement = statement.where(ModelConfig.id != exclude_config_id)
    return db.exec(statement).first() is not None


def _clear_default(db: Session, tenant_id: str) -> None:
    db.exec(
        update(ModelConfig)
        .where(
            ModelConfig.tenant_id == tenant_id,
            ModelConfig.is_default == True,  # noqa: E712 - SQLModel expression.
        )
        .values(is_default=False, updated_at=utc_now())
    )


def _validate_subscription_request(request: ModelConfigCreateRequest) -> None:
    if request.api_key:
        raise HTTPException(status_code=422, detail="MODEL_SUBSCRIPTION_API_KEY_FORBIDDEN")
    if request.base_url or request.provider or request.api_protocol:
        raise HTTPException(status_code=422, detail="MODEL_SUBSCRIPTION_DIRECT_CONFIG_FORBIDDEN")
    if request.extra_body or request.protocol_options:
        raise HTTPException(status_code=422, detail="MODEL_SUBSCRIPTION_DIRECT_CONFIG_FORBIDDEN")


def _subscription_account_response(
    operation: Callable[[], CodexSubscriptionAccount],
) -> CodexSubscriptionAccountRead:
    try:
        result = operation()
    except CodexSubscriptionError as exc:
        raise HTTPException(status_code=_subscription_error_status(exc.code), detail=exc.code) from exc
    return CodexSubscriptionAccountRead(**result.to_dict())


def _subscription_error_status(code: str) -> int:
    """将订阅运行时安全错误码映射为稳定 HTTP 状态，不暴露子进程细节。"""
    if code in {
        "MODEL_SUBSCRIPTION_BROWSER_UNAVAILABLE",
        "MODEL_SUBSCRIPTION_RUNTIME_UNAVAILABLE",
    }:
        return 503
    if code == "MODEL_SUBSCRIPTION_RUNTIME_TIMEOUT":
        return 504
    if code in {
        "MODEL_SUBSCRIPTION_RUNTIME_PROTOCOL_ERROR",
        "MODEL_SUBSCRIPTION_RUNTIME_FAILED",
    }:
        return 502
    return 503


def _validate_subscription_update_request(request: ModelConfigUpdateRequest) -> None:
    if request.api_key:
        raise HTTPException(status_code=422, detail="MODEL_SUBSCRIPTION_API_KEY_FORBIDDEN")
    if request.base_url or request.provider or request.api_protocol:
        raise HTTPException(status_code=422, detail="MODEL_SUBSCRIPTION_DIRECT_CONFIG_FORBIDDEN")
    if request.extra_body or request.protocol_options:
        raise HTTPException(status_code=422, detail="MODEL_SUBSCRIPTION_DIRECT_CONFIG_FORBIDDEN")


def _request_protocol_options(
    protocol_options: dict | None,
    protocol: ModelApiProtocol,
) -> dict:
    if protocol_options is None:
        return {}
    if protocol is ModelApiProtocol.OPENAI_CHAT_COMPLETIONS:
        return normalize_chat_protocol_options(protocol_options)
    if protocol_options:
        raise HTTPException(status_code=422, detail="MODEL_PROTOCOL_OPTIONS_INVALID")
    return {}


def _request_extra_body(extra_body: dict | None, protocol: ModelApiProtocol) -> dict:
    """Validate provider-specific request fields separately from protocol options."""
    if not extra_body:
        return {}
    if protocol is not ModelApiProtocol.OPENAI_CHAT_COMPLETIONS:
        raise HTTPException(status_code=422, detail="MODEL_EXTRA_BODY_UNSUPPORTED")
    return dict(extra_body)


def _validate_sampling(
    protocol: ModelApiProtocol, temperature: float, max_output_tokens: int
) -> None:
    max_temperature = 1 if protocol is ModelApiProtocol.ANTHROPIC_MESSAGES else 2
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
        configuration_revision=row.key_revision,
        protocol_options=current_protocol_options(row.protocol_options_json, protocol),
        security_revision=row.security_revision,
        model_mode=getattr(row, "auth_mode", ModelAuthMode.API_KEY),
    )


def _commit_or_conflict(db: Session) -> None:
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="MODEL_DEFAULT_CONFLICT") from exc
