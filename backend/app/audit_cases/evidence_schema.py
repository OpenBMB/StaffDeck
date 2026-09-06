from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ExtractedEvidence(BaseModel):
    audit_element_id: str
    evidence_type: Literal["conformity", "improvement", "nonconformity", "context"]
    evidence_text: str = Field(min_length=1, max_length=2_000)
    confidence: float = Field(ge=0.0, le=1.0)


class ChunkExtractionResult(BaseModel):
    chunk_id: str
    items: list[ExtractedEvidence] = Field(default_factory=list)


class ProcessingSummary(BaseModel):
    total: int
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0


class AuditEvidenceError(RuntimeError):
    pass


def validate_extraction_result(
    payload: dict[str, Any], *, allowed_element_ids: set[str]
) -> ChunkExtractionResult:
    result = ChunkExtractionResult.model_validate(payload)
    unknown = sorted(
        {
            item.audit_element_id
            for item in result.items
            if item.audit_element_id not in allowed_element_ids
        }
    )
    if unknown:
        raise AuditEvidenceError("INVALID_AUDIT_ELEMENT_REFERENCE:" + ",".join(unknown))
    return result
