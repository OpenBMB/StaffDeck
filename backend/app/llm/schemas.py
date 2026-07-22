from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class ModelConfigCreateRequest(BaseModel):
    tenant_id: str
    name: str
    provider: Optional[str] = None
    api_protocol: Optional[str] = None
    base_url: Optional[str] = None
    api_key: str = Field(default="", repr=False)
    model: str
    temperature: float = 0.2
    max_output_tokens: int = 8192
    extra_body: dict[str, Any] = Field(default_factory=dict)
    protocol_options: Optional[dict[str, Any]] = None
    is_default: bool = False
    enabled: bool = True


class ModelConfigUpdateRequest(BaseModel):
    tenant_id: str
    name: Optional[str] = None
    provider: Optional[str] = None
    api_protocol: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = Field(default=None, repr=False)
    model: Optional[str] = None
    temperature: Optional[float] = None
    max_output_tokens: Optional[int] = None
    extra_body: Optional[dict[str, Any]] = None
    protocol_options: Optional[dict[str, Any]] = None
    is_default: Optional[bool] = None
    enabled: Optional[bool] = None


class ModelConfigRead(BaseModel):
    id: str
    tenant_id: str
    name: str
    provider: str
    api_protocol: str
    base_url: Optional[str]
    api_key_masked: str
    model: str
    temperature: float
    max_output_tokens: int
    extra_body: dict[str, Any]
    protocol_options: dict[str, Any]
    legacy_unmapped_options: dict[str, Any]
    trust_status: str
    verification_attempt_status: str
    config_revision: int
    security_revision: int
    is_default: bool
    enabled: bool
    created_at: str
    updated_at: str

    model_config = ConfigDict(from_attributes=True)


class ModelCapabilityTestResult(BaseModel):
    id: str
    success: bool
    error_code: Optional[str] = None


class ModelConfigTestResponse(BaseModel):
    success: bool
    message: str
    activated: bool = False
    model: Optional[ModelConfigRead] = None
    output: Optional[str] = None
    attempt_id: Optional[str] = None
    trust_status: Optional[str] = None
    attempt_status: Optional[str] = None
    capabilities: list[ModelCapabilityTestResult] = Field(default_factory=list)
