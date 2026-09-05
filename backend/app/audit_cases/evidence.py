from __future__ import annotations

from collections.abc import Callable, Iterator

from pydantic import ValidationError
from sqlmodel import Session, delete, select

from app.audit_cases.elements import AuditElement, load_required_elements
from app.audit_cases.evidence_schema import (
    AuditEvidenceError,
    ChunkExtractionResult,
    ProcessingSummary,
    validate_extraction_result,
)
from app.db.models import (
    AuditCase,
    AuditCaseMaterialChunk,
    AuditEvidenceLedger,
    ModelConfig,
    utc_now,
)
from app.llm.client import LLMClient, LLMError

AUDIT_EVIDENCE_PROMPT = """
你是审核证据整理器。仅根据给定的单个材料块，提取能够支持审核要素判断的事实。
不得补写材料块没有提供的事实；无法支持要素时返回空 items。每条证据必须引用给定的审核要素 ID。
只返回 JSON：{"chunk_id":"...","items":[{"audit_element_id":"...","evidence_type":"conformity|improvement|nonconformity|context","evidence_text":"...","confidence":0.0}]}
""".strip()


ClientFactory = Callable[[ModelConfig], LLMClient]


class AuditEvidenceProcessor:
    EXTRACTOR_VERSION = "audit-evidence-v1"

    def __init__(self, db: Session, client_factory: ClientFactory = LLMClient) -> None:
        self.db = db
        self.client_factory = client_factory

    def process_pending_chunks(
        self, case: AuditCase, model_config: ModelConfig, batch_size: int = 4
    ) -> ProcessingSummary:
        elements = load_required_elements(case.management_systems_json)
        allowed = {item.id for item in elements}
        chunks = self._chunks_for_case(case.id, case.tenant_id)
        pending = [chunk for chunk in chunks if chunk.processing_status != "succeeded"]
        summary = ProcessingSummary(total=len(chunks), skipped=len(chunks) - len(pending))
        for batch in _batches(pending, batch_size):
            for chunk in batch:
                try:
                    result = self._extract_chunk(chunk, elements, model_config)
                    self._replace_chunk_evidence(case, chunk, result, allowed)
                    chunk.processing_status = "succeeded"
                    chunk.extracted_facts_json = [
                        item.model_dump(mode="json") for item in result.items
                    ]
                    chunk.updated_at = utc_now()
                    self.db.add(chunk)
                    self.db.commit()
                    summary.succeeded += 1
                except (LLMError, ValidationError, AuditEvidenceError) as exc:
                    self.db.rollback()
                    chunk.processing_status = "failed"
                    chunk.extracted_facts_json = [{"error_code": _evidence_error_code(exc)}]
                    chunk.updated_at = utc_now()
                    self.db.add(chunk)
                    self.db.commit()
                    summary.failed += 1
        return summary

    def _chunks_for_case(self, case_id: str, tenant_id: str) -> list[AuditCaseMaterialChunk]:
        return list(
            self.db.exec(
                select(AuditCaseMaterialChunk)
                .where(
                    AuditCaseMaterialChunk.tenant_id == tenant_id,
                    AuditCaseMaterialChunk.audit_case_id == case_id,
                )
                .order_by(AuditCaseMaterialChunk.material_id, AuditCaseMaterialChunk.chunk_index)
            ).all()
        )

    def _extract_chunk(
        self,
        chunk: AuditCaseMaterialChunk,
        elements: list[AuditElement],
        model_config: ModelConfig,
    ) -> ChunkExtractionResult:
        payload = self.client_factory(model_config).generate_json(
            AUDIT_EVIDENCE_PROMPT,
            {
                "chunk_id": chunk.id,
                "source_ref": f"{chunk.material_id}#chunk={chunk.chunk_index}",
                "content": chunk.content,
                "allowed_elements": [item.model_dump(mode="json") for item in elements],
            },
        )
        result = validate_extraction_result(
            payload, allowed_element_ids={item.id for item in elements}
        )
        if result.chunk_id != chunk.id:
            raise AuditEvidenceError("EXTRACTION_CHUNK_ID_MISMATCH")
        return result

    def _replace_chunk_evidence(
        self,
        case: AuditCase,
        chunk: AuditCaseMaterialChunk,
        result: ChunkExtractionResult,
        allowed_element_ids: set[str],
    ) -> None:
        validate_extraction_result(
            result.model_dump(mode="json"), allowed_element_ids=allowed_element_ids
        )
        self.db.exec(
            delete(AuditEvidenceLedger).where(
                AuditEvidenceLedger.tenant_id == case.tenant_id,
                AuditEvidenceLedger.audit_case_id == case.id,
                AuditEvidenceLedger.chunk_id == chunk.id,
                AuditEvidenceLedger.extractor_version == self.EXTRACTOR_VERSION,
            )
        )
        source_ref = f"{chunk.material_id}#chunk={chunk.chunk_index}"
        for item in result.items:
            self.db.add(
                AuditEvidenceLedger(
                    tenant_id=case.tenant_id,
                    audit_case_id=case.id,
                    audit_element_id=item.audit_element_id,
                    source_kind="case_material",
                    source_id=chunk.material_id,
                    source_version_id=f"{chunk.material_id}:current",
                    chunk_id=chunk.id,
                    source_ref=source_ref,
                    evidence_type=item.evidence_type,
                    evidence_text=item.evidence_text,
                    confidence=item.confidence,
                    extractor_version=self.EXTRACTOR_VERSION,
                )
            )


def _batches(
    rows: list[AuditCaseMaterialChunk], size: int
) -> Iterator[list[AuditCaseMaterialChunk]]:
    if size < 1:
        raise ValueError("batch_size must be positive")
    for start in range(0, len(rows), size):
        yield rows[start : start + size]


def _evidence_error_code(exc: Exception) -> str:
    text = str(exc)
    if text.startswith("INVALID_AUDIT_ELEMENT_REFERENCE"):
        return "INVALID_AUDIT_ELEMENT_REFERENCE"
    if isinstance(exc, ValidationError):
        return "INVALID_EXTRACTION_JSON"
    if isinstance(exc, LLMError):
        return "EVIDENCE_MODEL_FAILED"
    return "EVIDENCE_PROCESSING_FAILED"
