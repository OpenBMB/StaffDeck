from __future__ import annotations

from app.core.task_request_compiler import CapabilityDescriptor


def audit_capability_descriptors(
    audit_case_id: str | None,
) -> list[CapabilityDescriptor]:
    if not audit_case_id:
        return []
    scope_metadata = {
        "provider": "audit_case",
        "audit_case_scoped": True,
        "audit_case_id": audit_case_id,
    }
    return [
        CapabilityDescriptor(
            capability_id="audit.case.manifest",
            name="audit_case_manifest",
            kind="internal",
            capability_scope="sop_specific",
            description="读取当前审核项目的材料、版本和处理状态摘要，不返回材料全文。",
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            metadata={**scope_metadata, "side_effect": "read"},
        ),
        CapabilityDescriptor(
            capability_id="audit.evidence.process",
            name="audit_evidence_process",
            kind="internal",
            capability_scope="sop_specific",
            description="处理当前审核项目尚未完成的材料块证据和固定版本知识检索。",
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            metadata={**scope_metadata, "side_effect": "write"},
        ),
        CapabilityDescriptor(
            capability_id="audit.report.status",
            name="audit_report_status",
            kind="internal",
            capability_scope="sop_specific",
            description="读取当前审核项目的覆盖率、报告版本和章节处理状态。",
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            metadata={**scope_metadata, "side_effect": "read"},
        ),
    ]
