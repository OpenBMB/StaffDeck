from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

RuleExecutionLevel = Literal["mandatory", "warning", "guidance"]
RuleExecutionMethod = Literal["deterministic", "model_assisted"]


class RuleSetCreate(BaseModel):
    model_config = ConfigDict(frozen=True)

    tenant_id: str
    key: str
    name: str
    description: str = ""
    management_systems: list[str] = Field(default_factory=list)
    audit_types: list[str] = Field(default_factory=list)
    business_domain: str = ""


class RuleDefinitionCreate(BaseModel):
    model_config = ConfigDict(frozen=True)

    rule_key: str
    name: str
    description: str = ""
    workflow_nodes: list[str] = Field(default_factory=list)
    information_domains: list[str] = Field(default_factory=list)
    document_types: list[str] = Field(default_factory=list)
    field_keys: list[str] = Field(default_factory=list)
    execution_level: RuleExecutionLevel = "guidance"
    execution_method: RuleExecutionMethod = "deterministic"
    condition: dict[str, Any] = Field(default_factory=dict)
    input_requirements: list[dict[str, Any]] = Field(default_factory=list)
    evidence_requirements: list[dict[str, Any]] = Field(default_factory=list)
    source_refs: list[dict[str, Any]] = Field(default_factory=list)
    sequence: Any = 0
    enabled: Any = True


class RuleValidationError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class RuleAccessDenied(PermissionError):
    def __init__(self, message: str = "RULE_ADMIN_REQUIRED"):
        super().__init__(message)


class RuleVersionImmutableError(RuntimeError):
    def __init__(self, message: str = "RULE_VERSION_IMMUTABLE"):
        super().__init__(message)
