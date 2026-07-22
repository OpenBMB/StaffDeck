from __future__ import annotations

import copy
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal, Mapping

from fastapi import HTTPException
from sqlmodel import Session

from app.db.models import ModelConfig
from app.llm.model_protocols import (
    ModelApiProtocol,
    current_protocol_options,
    model_config_fingerprint,
)


@dataclass(frozen=True)
class ResolvedModelConfig:
    id: str
    tenant_id: str
    api_protocol: ModelApiProtocol
    base_url: str | None
    api_key_encrypted: str
    model: str
    temperature: float
    max_output_tokens: int
    protocol_options: Mapping[str, Any]
    legacy_extra_body: Mapping[str, Any]
    config_revision: int
    security_revision: int
    purpose: Literal["runtime", "verification"]
    timeout_seconds: float | None = None


def resolve_model_config_for_runtime(
    db: Session, tenant_id: str, config_id: str
) -> ResolvedModelConfig:
    row = _current_model_config(db, tenant_id, config_id)
    if not row.enabled:
        raise HTTPException(status_code=409, detail="MODEL_CONFIG_DISABLED")
    protocol = _protocol(row)
    if row.trust_status == "legacy_trusted" or _is_implicit_legacy_openai(row, protocol):
        if protocol is not ModelApiProtocol.OPENAI_CHAT_COMPLETIONS:
            raise HTTPException(status_code=409, detail="MODEL_CONFIG_VERIFICATION_REQUIRED")
    elif row.trust_status != "verified" or row.verified_fingerprint != _fingerprint(row):
        raise HTTPException(status_code=409, detail="MODEL_CONFIG_VERIFICATION_REQUIRED")
    return _snapshot(row, protocol, purpose="runtime")


def resolve_model_config_for_verification(
    db: Session, tenant_id: str, config_id: str, attempt_id: str
) -> ResolvedModelConfig:
    row = _current_model_config(db, tenant_id, config_id)
    if (
        row.verification_attempt_id != attempt_id
        or row.verification_attempt_status != "verifying"
    ):
        raise HTTPException(status_code=409, detail="MODEL_VERIFICATION_STALE")
    protocol = _protocol(row)
    return _snapshot(row, protocol, purpose="verification")


def _current_model_config(db: Session, tenant_id: str, config_id: str) -> ModelConfig:
    row = db.get(ModelConfig, config_id)
    if row is None or row.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="MODEL_CONFIG_NOT_FOUND")
    db.refresh(row)
    return row


def _protocol(row: ModelConfig) -> ModelApiProtocol:
    try:
        return ModelApiProtocol(row.api_protocol)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="MODEL_PROTOCOL_UNSUPPORTED") from exc


def _snapshot(
    row: ModelConfig,
    protocol: ModelApiProtocol,
    *,
    purpose: Literal["runtime", "verification"],
) -> ResolvedModelConfig:
    options = current_protocol_options(row.protocol_options_json, protocol)
    legacy_extra_body: dict[str, Any] = {}
    if purpose == "runtime" and (
        row.trust_status == "legacy_trusted" or _is_implicit_legacy_openai(row, protocol)
    ):
        legacy_extra_body = copy.deepcopy(row.extra_body_json or {})
    return ResolvedModelConfig(
        id=row.id,
        tenant_id=row.tenant_id,
        api_protocol=protocol,
        base_url=row.base_url,
        api_key_encrypted=row.api_key_encrypted,
        model=row.model,
        temperature=row.temperature,
        max_output_tokens=row.max_output_tokens,
        protocol_options=_freeze(options),
        legacy_extra_body=_freeze(legacy_extra_body),
        config_revision=row.config_revision,
        security_revision=row.security_revision,
        purpose=purpose,
        timeout_seconds=None,
    )


def _fingerprint(row: ModelConfig) -> str:
    protocol = _protocol(row)
    return model_config_fingerprint(
        api_protocol=row.api_protocol,
        base_url=row.base_url,
        model=row.model,
        key_revision=row.key_revision,
        protocol_options=current_protocol_options(row.protocol_options_json, protocol),
        security_revision=row.security_revision,
    )


def _is_implicit_legacy_openai(row: ModelConfig, protocol: ModelApiProtocol) -> bool:
    """Keep pre-protocol ORM/fixture rows runnable during the migration window."""
    return (
        protocol is ModelApiProtocol.OPENAI_CHAT_COMPLETIONS
        and row.trust_status == "unverified"
        and row.verified_fingerprint is None
        and row.security_revision == 1
        and row.config_revision == 1
        and row.verification_attempt_status in {None, "idle"}
    )


def _freeze(value: dict[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType({key: _freeze_value(item) for key, item in copy.deepcopy(value).items()})


def _freeze_value(value: Any) -> Any:
    if isinstance(value, dict):
        return _freeze(value)
    if isinstance(value, list):
        return tuple(_freeze_value(item) for item in value)
    return value


def snapshot_model_config(
    model_config: Any, *, min_output_tokens: int = 0
) -> ResolvedModelConfig:
    if isinstance(model_config, ResolvedModelConfig):
        if model_config.max_output_tokens >= min_output_tokens:
            return model_config
        return ResolvedModelConfig(
            **{
                **model_config.__dict__,
                "max_output_tokens": min_output_tokens,
            }
        )
    protocol = ModelApiProtocol(
        getattr(model_config, "api_protocol", "openai_chat_completions")
    )
    return ResolvedModelConfig(
        id=str(getattr(model_config, "id", "")),
        tenant_id=str(getattr(model_config, "tenant_id", "")),
        api_protocol=protocol,
        purpose=getattr(model_config, "purpose", "runtime"),
        api_key_encrypted=model_config.api_key_encrypted,
        base_url=model_config.base_url,
        model=model_config.model,
        temperature=model_config.temperature,
        max_output_tokens=max(
            int(getattr(model_config, "max_output_tokens", 0) or 0), min_output_tokens
        ),
        protocol_options=_freeze(
            _snapshot_protocol_options(model_config, protocol)
        ),
        legacy_extra_body=_freeze(
            copy.deepcopy(
                getattr(model_config, "legacy_extra_body", {})
                or getattr(model_config, "extra_body_json", {})
            )
        ),
        config_revision=getattr(model_config, "config_revision", 1),
        security_revision=getattr(model_config, "security_revision", 1),
        timeout_seconds=getattr(model_config, "timeout_seconds", None),
    )


def _snapshot_protocol_options(model_config: Any, protocol: ModelApiProtocol) -> dict[str, Any]:
    direct = getattr(model_config, "protocol_options", {})
    if isinstance(direct, dict) and direct:
        return copy.deepcopy(direct)
    return current_protocol_options(
        getattr(model_config, "protocol_options_json", {}), protocol
    )
