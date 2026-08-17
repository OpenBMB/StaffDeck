from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


EvolutionResourceType = Literal["sop", "general_skill"]


class EvolutionAnalyzeRequest(BaseModel):
    tenant_id: str
    resource_type: EvolutionResourceType | None = None
    resource_id: str | None = None
    feedback_ids: list[str] = Field(default_factory=list)
    instruction: str | None = None


class EvolutionRejectRequest(BaseModel):
    tenant_id: str
    reason: str = ""


class EvolutionActionRequest(BaseModel):
    tenant_id: str


class EvolutionProposalRead(BaseModel):
    id: str
    tenant_id: str
    agent_id: str
    resource_type: EvolutionResourceType
    resource_id: str
    resource_key: str
    resource_name: str
    base_version: str | None = None
    status: str
    trigger_type: str
    risk_level: str
    hypothesis: str
    rationale: str
    expected_outcome: str
    source_feedback_ids: list[str] = Field(default_factory=list)
    evidence: list[dict] = Field(default_factory=list)
    candidate: dict = Field(default_factory=dict)
    diff: list[dict] = Field(default_factory=list)
    evaluation: dict = Field(default_factory=dict)
    error: str | None = None
    created_by_user_id: str
    reviewed_by_user_id: str | None = None
    created_at: str
    updated_at: str
    reviewed_at: str | None = None
    published_at: str | None = None
    rolled_back_at: str | None = None
