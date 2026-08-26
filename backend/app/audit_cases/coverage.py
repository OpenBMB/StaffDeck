from __future__ import annotations

from itertools import pairwise

from sqlmodel import Session, select

from app.audit_cases.elements import load_required_elements
from app.audit_cases.schema import AuditCoverageSnapshot
from app.db.models import (
    AuditCase,
    AuditCaseMaterial,
    AuditCaseMaterialChunk,
    AuditElementCoverage,
)

RESOLVED_ELEMENT_STATUSES = {
    "evidence_found",
    "knowledge_found",
    "evidence_gap",
    "not_applicable",
}


class AuditCoverageError(RuntimeError):
    pass


def calculate_coverage(db: Session, case: AuditCase) -> AuditCoverageSnapshot:
    materials = list(
        db.exec(
            select(AuditCaseMaterial).where(
                AuditCaseMaterial.tenant_id == case.tenant_id,
                AuditCaseMaterial.audit_case_id == case.id,
                AuditCaseMaterial.is_current,
            )
        ).all()
    )
    material_ids = {material.id for material in materials}
    chunks = (
        list(
            db.exec(
                select(AuditCaseMaterialChunk).where(
                    AuditCaseMaterialChunk.tenant_id == case.tenant_id,
                    AuditCaseMaterialChunk.audit_case_id == case.id,
                    AuditCaseMaterialChunk.material_id.in_(material_ids),
                )
            ).all()
        )
        if material_ids
        else []
    )
    elements = [
        element
        for element in load_required_elements(case.management_systems_json)
        if element.required
    ]
    coverage_rows = list(
        db.exec(
            select(AuditElementCoverage).where(
                AuditElementCoverage.tenant_id == case.tenant_id,
                AuditElementCoverage.audit_case_id == case.id,
            )
        ).all()
    )
    coverage_by_element = {row.audit_element_id: row for row in coverage_rows}

    files_total = len(materials)
    files_succeeded = sum(
        material.extraction_status == "succeeded"
        and material.processing_status == "succeeded"
        for material in materials
    )
    chunks_total = len(chunks)
    chunks_succeeded = sum(chunk.processing_status == "succeeded" for chunk in chunks)
    elements_total = len(elements)
    elements_resolved = sum(
        coverage_by_element.get(element.id) is not None
        and coverage_by_element[element.id].status in RESOLVED_ELEMENT_STATUSES
        for element in elements
    )

    blockers: list[str] = []
    file_coverage = _coverage_ratio(files_succeeded, files_total)
    chunk_coverage = _coverage_ratio(chunks_succeeded, chunks_total)
    element_coverage = _coverage_ratio(elements_resolved, elements_total)
    if files_total == 0:
        blockers.append("NO_CURRENT_MATERIALS")
    elif file_coverage < 1.0:
        blockers.append("FILE_COVERAGE_INCOMPLETE")
    if chunks_total == 0:
        if files_total:
            blockers.append("CHUNK_COVERAGE_INCOMPLETE")
    elif chunk_coverage < 1.0:
        blockers.append("CHUNK_COVERAGE_INCOMPLETE")
    blockers.extend(_interval_blockers(materials, chunks))
    if elements_total == 0:
        blockers.append("NO_REQUIRED_AUDIT_ELEMENTS")
    elif element_coverage < 1.0:
        blockers.append("ELEMENT_COVERAGE_INCOMPLETE")

    return AuditCoverageSnapshot(
        file_coverage=file_coverage,
        chunk_coverage=chunk_coverage,
        element_coverage=element_coverage,
        publish_allowed=not blockers,
        blockers=blockers,
        files_total=files_total,
        files_succeeded=files_succeeded,
        chunks_total=chunks_total,
        chunks_succeeded=chunks_succeeded,
        elements_total=elements_total,
        elements_resolved=elements_resolved,
    )


def require_publishable(snapshot: AuditCoverageSnapshot) -> None:
    if not snapshot.publish_allowed:
        raise AuditCoverageError("AUDIT_COVERAGE_BLOCKED:" + ",".join(snapshot.blockers))


def _coverage_ratio(succeeded: int, total: int) -> float:
    return succeeded / total if total else 0.0


def _interval_blockers(
    materials: list[AuditCaseMaterial], chunks: list[AuditCaseMaterialChunk]
) -> list[str]:
    for material in materials:
        material_chunks = sorted(
            (chunk for chunk in chunks if chunk.material_id == material.id),
            key=lambda chunk: chunk.chunk_index,
        )
        if not material_chunks or _has_interval_gap(material.characters, material_chunks):
            return ["CHUNK_INTERVAL_GAP"]
    return []


def _has_interval_gap(characters: int, chunks: list[AuditCaseMaterialChunk]) -> bool:
    if chunks[0].start_char != 0:
        return True
    for previous, current in pairwise(chunks):
        if previous.end_char != current.start_char:
            return True
    return chunks[-1].end_char != characters
