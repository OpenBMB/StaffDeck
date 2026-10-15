"""Full wire DTOs; importing clients never loads storage or domain implementations."""
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field
from .capability_scope import CapabilityScope


class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SpaceCreate(Payload):
    name: str = Field(min_length=1, max_length=200)
    id: str | None = None


class BaseCreate(Payload):
    space_id: str = "local"
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    capability_scope: CapabilityScope = "general"
    metadata: dict[str, Any] = Field(default_factory=dict)


class Upload(Payload):
    space_id: str = "local"
    filename: str
    content_base64: str
    title: str | None = None
    model_id: str | None = None
    capability_scope: CapabilityScope = "general"
    metadata: dict[str, Any] = Field(default_factory=dict)


class Edit(Payload):
    expected_updated_at: str
    content_md: str | None = None
    title: str | None = None
    status: Literal["ready", "processing", "failed", "archived"] | None = None
    metadata: dict[str, Any] | None = None


class QueryRequest(Payload):
    space_id: str = "local"
    query: str
    knowledge_base_ids: list[str] = Field(default_factory=list)
    version_ids: list[str] = Field(default_factory=list)
    max_chunks: int = Field(default=8, ge=1, le=100)
    max_buckets: int = Field(default=4, ge=1, le=100)
    use_model: bool = True
    model_id: str | None = None
    query_type: Literal["answer", "policy_check", "interface_discovery", "procedure_discovery"] = "answer"
    desired_evidence: str | None = None
    scope: dict[str, Any] = Field(default_factory=dict)
    mode: Literal["chat", "discovery", "debug"] = "chat"
    document_ids: list[str] = Field(default_factory=list)
    max_bucket_rounds: int = Field(default=2, ge=1, le=10)
    budget_tokens: int = Field(default=4000, ge=1)
    max_depth: int = Field(default=2, ge=1, le=10)
    need_evidence_pack: bool = True


class VersionAction(Payload):
    version_id: str | None = None
    version_label: str | None = Field(default=None, min_length=1, max_length=200)
    empty: bool = False
    preserve_metadata: bool = False
    name: str | None = None
    description: str | None = None


class BindingAction(Payload):
    space_id: str
    hidden: bool = False


class BindingEdit(Payload):
    hidden: bool | None = None
    status: str | None = None
    metadata: dict[str, Any] | None = None


class BaseEdit(Payload):
    expected_updated_at: str
    name: str | None = None
    description: str | None = None
    status: str | None = None
    capability_scope: CapabilityScope | None = None
    metadata: dict[str, Any] | None = None


class LayerEdit(Payload):
    expected_updated_at: str
    content_md: str | None = None
    title: str | None = None
    summary: str | None = None
    metadata: dict[str, Any] | None = None
    refresh_sources: bool = False


class ConceptEdit(Payload):
    expected_updated_at: str | None = None
    content_md: str
    document_id: str | None = None
    status: Literal["active", "archived", "deleted"] = "active"
