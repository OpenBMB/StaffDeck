from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class ModelConfigCreateRequest(BaseModel):
    tenant_id: str
    name: str
    provider: str = "openai_compatible"
    base_url: Optional[str] = None
    api_key: str = Field(default="", repr=False)
    model: str
    temperature: float = 0.2
    max_output_tokens: int = 8192
    extra_body: dict[str, Any] = Field(default_factory=dict)
    is_default: bool = False
    enabled: bool = True


class ModelConfigUpdateRequest(BaseModel):
    tenant_id: str
    name: Optional[str] = None
    provider: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = Field(default=None, repr=False)
    model: Optional[str] = None
    temperature: Optional[float] = None
    max_output_tokens: Optional[int] = None
    extra_body: Optional[dict[str, Any]] = None
    is_default: Optional[bool] = None
    enabled: Optional[bool] = None


class ModelConfigRead(BaseModel):
    id: str
    tenant_id: str
    name: str
    provider: str
    base_url: Optional[str]
    api_key_masked: str
    model: str
    temperature: float
    max_output_tokens: int
    extra_body: dict[str, Any]
    is_default: bool
    enabled: bool
    created_at: str
    updated_at: str

    model_config = ConfigDict(from_attributes=True)


class ModelConfigTestResponse(BaseModel):
    success: bool
    message: str
    output: Optional[str] = None
