from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable, Iterable
from io import BytesIO

from docx import Document
from pydantic import BaseModel
from sqlmodel import Session, select

from app.audit_cases.coverage import calculate_coverage, require_publishable
from app.audit_cases.elements import load_required_elements
from app.audit_cases.storage import read_case_blob, write_case_blob
from app.db.models import (
    AuditCase,
    AuditCaseMaterial,
    AuditEvidenceLedger,
    AuditReportSection,
    AuditReportVersion,
    ModelConfig,
)
from app.llm.client import LLMClient, LLMError

REPORT_SECTION_PROMPT = """
你是审核报告章节撰写器。只使用输入证据台账中的事实，不得补写、推测或引用未提供的资料。
每项事实必须使用 [EVIDENCE:<id>] 标注来源；只返回章节正文 Markdown。
""".strip()
EVIDENCE_CITATION = re.compile(r"\[EVIDENCE:([A-Za-z0-9_-]+)\]")
TEMPLATE_ANCHOR = re.compile(r"\{\{SECTION:([A-Za-z0-9_-]+)\}\}")


ClientFactory = Callable[[ModelConfig], LLMClient]


class ReportSectionSpec(BaseModel):
    id: str
    title: str
    sequence: int
    audit_element_ids: list[str]


class AuditCitationError(RuntimeError):
    pass


class AuditReportBlocked(RuntimeError):
    pass


class AuditReportService:
    def __init__(self, db: Session, client_factory: ClientFactory = LLMClient) -> None:
        self.db = db
        self.client_factory = client_factory

    def create_version(self, case: AuditCase) -> AuditReportVersion:
        latest = self.db.exec(
            select(AuditReportVersion)
            .where(
                AuditReportVersion.tenant_id == case.tenant_id,
                AuditReportVersion.audit_case_id == case.id,
            )
            .order_by(AuditReportVersion.version.desc())
        ).first()
        version = (latest.version + 1) if latest else 1
        materials = list(
            self.db.exec(
                select(AuditCaseMaterial).where(
                    AuditCaseMaterial.tenant_id == case.tenant_id,
                    AuditCaseMaterial.audit_case_id == case.id,
                    AuditCaseMaterial.is_current,
                )
            ).all()
        )
        report = AuditReportVersion(
            tenant_id=case.tenant_id,
            audit_case_id=case.id,
            version=version,
            material_version_ids_json=[f"{row.id}:v{row.version}" for row in materials],
            knowledge_base_version_ids_json=list(case.knowledge_base_version_ids_json),
        )
        self.db.add(report)
        self.db.flush()
        for spec in _report_section_specs(case.management_systems_json):
            self.db.add(
                AuditReportSection(
                    tenant_id=case.tenant_id,
                    audit_case_id=case.id,
                    report_version_id=report.id,
                    section_id=spec.id,
                    title=spec.title,
                    sequence=spec.sequence,
                    audit_element_ids_json=list(spec.audit_element_ids),
                )
            )
        self.db.commit()
        self.db.refresh(report)
        return report

    def generate_pending_sections(
        self, case: AuditCase, report: AuditReportVersion, model_config: ModelConfig
    ) -> None:
        for section in self._pending_sections(report.id):
            evidence = self._section_evidence(case.id, section.audit_element_ids_json)
            try:
                draft = self.client_factory(model_config).generate_text(
                    REPORT_SECTION_PROMPT,
                    {
                        "section_id": section.section_id,
                        "title": section.title,
                        "evidence": [ledger_public_payload(item) for item in evidence],
                        "citation_rule": "每项事实使用 [EVIDENCE:<id>] 引用",
                    },
                )
                citation_ids = validate_section_citations(draft, evidence)
                section.draft_markdown = draft
                section.citation_ids_json = citation_ids
                section.status = "succeeded"
                section.error_code = None
            except (LLMError, AuditCitationError) as exc:
                section.status = "failed"
                section.retry_count += 1
                section.error_code = (
                    "REPORT_CITATION_INVALID"
                    if isinstance(exc, AuditCitationError)
                    else "REPORT_MODEL_FAILED"
                )
            self.db.add(section)
            self.db.commit()

    def assemble_draft(self, report: AuditReportVersion) -> str:
        sections = self._sections(report.id)
        if any(row.status != "succeeded" for row in sections):
            raise AuditReportBlocked("REPORT_SECTIONS_INCOMPLETE")
        return "\n\n".join(
            f"## {row.title}\n\n{row.draft_markdown}"
            for row in sorted(sections, key=lambda item: item.sequence)
        )

    def publish(
        self, case: AuditCase, report: AuditReportVersion, confirmed_by: str | None
    ) -> AuditReportVersion:
        snapshot = calculate_coverage(self.db, case)
        require_publishable(snapshot)
        markdown = self.assemble_draft(report)
        filename = "审核报告.docx" if confirmed_by else "审核报告-待确认草稿.docx"
        template = self.db.exec(
            select(AuditCaseMaterial).where(
                AuditCaseMaterial.tenant_id == case.tenant_id,
                AuditCaseMaterial.audit_case_id == case.id,
                AuditCaseMaterial.material_type == "report_template",
                AuditCaseMaterial.is_current,
            )
        ).first()
        if template is None:
            data = render_docx(markdown, title=filename.removesuffix(".docx"))
        else:
            sections = {
                row.section_id: row.draft_markdown for row in self._sections(report.id)
            }
            data = render_docx_from_template(read_case_blob(template.storage_key), sections)
        report.final_storage_key = write_case_blob(
            tenant_id=case.tenant_id,
            audit_case_id=case.id,
            material_id=report.id,
            name=filename,
            data=data,
        )
        report.coverage_snapshot_json = snapshot.model_dump(mode="json")
        report.status = "published" if confirmed_by else "review"
        report.lead_auditor_confirmed_by = confirmed_by
        self.db.add(report)
        self.db.commit()
        self.db.refresh(report)
        return report

    def _pending_sections(self, report_id: str) -> list[AuditReportSection]:
        return list(
            self.db.exec(
                select(AuditReportSection)
                .where(
                    AuditReportSection.report_version_id == report_id,
                    AuditReportSection.status.in_(["pending", "failed"]),
                )
                .order_by(AuditReportSection.sequence)
            ).all()
        )

    def _sections(self, report_id: str) -> list[AuditReportSection]:
        return list(
            self.db.exec(
                select(AuditReportSection)
                .where(AuditReportSection.report_version_id == report_id)
                .order_by(AuditReportSection.sequence)
            ).all()
        )

    def _section_evidence(
        self, case_id: str, audit_element_ids: list[str]
    ) -> list[AuditEvidenceLedger]:
        if not audit_element_ids:
            return []
        return list(
            self.db.exec(
                select(AuditEvidenceLedger)
                .where(
                    AuditEvidenceLedger.audit_case_id == case_id,
                    AuditEvidenceLedger.audit_element_id.in_(audit_element_ids),
                )
                .order_by(AuditEvidenceLedger.audit_element_id, AuditEvidenceLedger.created_at)
            ).all()
        )


def _report_section_specs(management_systems: list[str]) -> list[ReportSectionSpec]:
    grouped: dict[str, list[str]] = defaultdict(list)
    titles: dict[str, str] = {}
    for element in load_required_elements(management_systems):
        if element.report_section_id not in titles:
            titles[element.report_section_id] = element.title
        grouped[element.report_section_id].append(element.id)
    return [
        ReportSectionSpec(
            id=section_id,
            title=titles[section_id],
            sequence=sequence,
            audit_element_ids=sorted(element_ids),
        )
        for sequence, (section_id, element_ids) in enumerate(sorted(grouped.items()), start=1)
    ]


def ledger_public_payload(row: AuditEvidenceLedger) -> dict[str, object]:
    return {
        "id": row.id,
        "audit_element_id": row.audit_element_id,
        "source_kind": row.source_kind,
        "source_ref": row.source_ref,
        "source_version_id": row.source_version_id,
        "evidence_type": row.evidence_type,
        "evidence_text": row.evidence_text,
        "confidence": row.confidence,
    }


def validate_section_citations(
    markdown: str, evidence: list[AuditEvidenceLedger]
) -> list[str]:
    allowed = {item.id for item in evidence}
    cited = EVIDENCE_CITATION.findall(markdown)
    if not cited or any(item not in allowed for item in cited):
        raise AuditCitationError("REPORT_CITATION_INVALID")
    return list(dict.fromkeys(cited))


def render_docx(markdown: str, *, title: str) -> bytes:
    document = Document()
    document.add_heading(title, level=0)
    for block in markdown.split("\n\n"):
        if block.startswith("## "):
            document.add_heading(block.removeprefix("## "), level=1)
        elif block.strip():
            document.add_paragraph(block)
    stream = BytesIO()
    document.save(stream)
    return stream.getvalue()


def render_docx_from_template(
    template_bytes: bytes, sections: dict[str, str]
) -> bytes:
    document = Document(BytesIO(template_bytes))
    paragraphs = list(_iter_paragraphs(document))
    document_text = "\n".join(paragraph.text for paragraph in paragraphs)
    anchors = TEMPLATE_ANCHOR.findall(document_text)
    expected = set(sections)
    if len(anchors) != len(expected) or set(anchors) != expected or len(anchors) != len(set(anchors)):
        raise AuditReportBlocked("REPORT_TEMPLATE_ANCHOR_MISSING")
    for paragraph in paragraphs:
        for section_id, markdown in sections.items():
            anchor = f"{{{{SECTION:{section_id}}}}}"
            if anchor in paragraph.text:
                paragraph.text = paragraph.text.replace(anchor, markdown)
    stream = BytesIO()
    document.save(stream)
    return stream.getvalue()


def _iter_paragraphs(document: Document) -> Iterable[object]:
    yield from document.paragraphs
    for table in document.tables:
        for row in table.rows:
            for cell in row.cells:
                yield from cell.paragraphs
