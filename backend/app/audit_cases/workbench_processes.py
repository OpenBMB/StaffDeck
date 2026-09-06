"""Certification workbench process definitions and document-scoped gates."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypedDict

from sqlmodel import Session, select

from app.db.models import AuditCase, AuditWorkItem
from app.db.workbench_checks import AuditDocumentCheck

PROCESS_NAMES = (
    "信息收集",
    "信息交流",
    "申请受理确认",
    "申请评审(含转机构申请、初审、再认证、变更）",
    "审批",
    "申请受理通知书（审批通过）/申请不予受理通知书（审批不通过）",
    "合同签订/合同评审",
    "审核方案策划",
    "基准人日和结合人日计算",
    "审核方案审核和批准",
    "审核方案交底",
    "审核/审查任务调度",
    "审核/审查任务发布和接受",
    "审核任务的CNCA报送和跟踪",
    "审核计划编制和批准",
    "审核计划发布和确认",
    "审核计划实施",
    "现场审核",
    "不符合和关闭",
    "审核报告",
    "审核方案检查",
    "审核方案改进",
    "现场审核结束",
    "资料完整性校验",
    "资料内容准确性校验",
    "认证决定（评定）",
    "认证决定结果报告",
    "审核方案调整",
    "证书制作/制证",
    "证书签发",
    "证书信息报送",
    "监督保持活动",
    "再认证保持活动",
    "证书状态变更管理",
    "全流程记录归档",
    "认证档案调阅",
)

ENABLED_PROCESS_NUMBERS = frozenset(
    {15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26}
)


@dataclass(frozen=True)
class ProcessDefinition:
    number: int
    name: str
    stage: int
    enabled: bool
    predecessor_numbers: tuple[int, ...] = ()
    required_reference_process_numbers: tuple[int, ...] = ()
    requires_fresh_check: bool = False
    guidance: str = ""


class ProcessGateBlocker(TypedDict, total=False):
    code: str
    message: str
    process_number: int


class ProcessGatePredecessor(TypedDict, total=False):
    number: int
    name: str
    status: str
    approved: bool
    work_item_id: str | None
    document_id: str | None
    document_version_id: str | None


class ProcessGate(TypedDict):
    process_number: int
    enabled: bool
    ready: bool
    blockers: list[ProcessGateBlocker]
    predecessors: list[ProcessGatePredecessor]
    required_reference_document_ids: list[str]
    check: dict[str, Any] | None


_SPECIAL_DEFINITIONS: dict[int, dict[str, Any]] = {
    17: {
        "predecessor_numbers": (16,),
        "required_reference_process_numbers": (16,),
        "guidance": "先确认审核计划已发布并由相关人员确认，再进入计划实施。",
    },
    21: {
        "predecessor_numbers": (17,),
        "required_reference_process_numbers": (17,),
        "requires_fresh_check": True,
        "guidance": "对当前 AuditCaseDocument 执行文件检查，并人工复核所有警告。",
    },
    22: {
        "predecessor_numbers": (21,),
        "required_reference_process_numbers": (21,),
        "guidance": "针对审核方案检查发现形成改进记录，并回链检查依据。",
    },
    23: {
        "predecessor_numbers": (18,),
        "required_reference_process_numbers": (18,),
        "guidance": "现场审核完成后记录结束情况；不符合项关闭可在后续继续。",
    },
}


def _stage(number: int) -> int:
    return 1 if number <= 7 else 2 if number <= 23 else 3


def all_process_definitions() -> tuple[ProcessDefinition, ...]:
    return tuple(
        ProcessDefinition(
            number=number,
            name=name,
            stage=_stage(number),
            enabled=number in ENABLED_PROCESS_NUMBERS,
            **_SPECIAL_DEFINITIONS.get(number, {}),
        )
        for number, name in enumerate(PROCESS_NAMES, 1)
    )


def get_process_definition(number: int) -> ProcessDefinition:
    if number < 1 or number > len(PROCESS_NAMES):
        raise KeyError(number)
    return all_process_definitions()[number - 1]


def _blocker(code: str, message: str, process_number: int | None = None) -> ProcessGateBlocker:
    value: ProcessGateBlocker = {"code": code, "message": message}
    if process_number is not None:
        value["process_number"] = process_number
    return value


def evaluate_process_gate(
    db: Session, case: AuditCase, process_number: int, document_id: str | None = None
) -> ProcessGate:
    definition = get_process_definition(process_number)
    blockers: list[ProcessGateBlocker] = []
    predecessors: list[ProcessGatePredecessor] = []
    required_reference_document_ids: list[str] = []
    check: dict[str, Any] | None = None

    if not definition.enabled:
        blockers.append(_blocker("PROCESS_NOT_ENABLED", "该流程仍处于后续阶段。"))

    for predecessor_number in definition.predecessor_numbers:
        candidates = db.exec(
            select(AuditWorkItem)
            .where(
                AuditWorkItem.tenant_id == case.tenant_id,
                AuditWorkItem.audit_case_id == case.id,
                AuditWorkItem.process_number == predecessor_number,
                AuditWorkItem.status == "approved",
            )
            .order_by(AuditWorkItem.created_at.desc(), AuditWorkItem.id.desc())
        ).all()
        selected = next((row for row in candidates if row.document_id == document_id), None)
        selected = selected or (candidates[0] if candidates else None)
        predecessor: ProcessGatePredecessor = {
            "number": predecessor_number,
            "name": get_process_definition(predecessor_number).name,
            "status": "approved" if selected else "missing",
            "approved": selected is not None,
            "work_item_id": selected.id if selected else None,
            "document_id": selected.document_id if selected else None,
            "document_version_id": selected.approved_version_id if selected else None,
        }
        predecessors.append(predecessor)
        if selected is None:
            blockers.append(
                _blocker(
                    "PROCESS_PRECONDITION_REQUIRED",
                    f"流程 {predecessor_number} 尚未内部复核通过。",
                    predecessor_number,
                )
            )
        elif document_id and selected.document_id != document_id:
            required_reference_document_ids.append(selected.document_id)

    if definition.requires_fresh_check:
        if not document_id:
            blockers.append(_blocker("CHECK_REQUIRED", "需要选择主文件后执行当前文件检查。"))
        else:
            latest = db.exec(
                select(AuditDocumentCheck)
                .where(
                    AuditDocumentCheck.tenant_id == case.tenant_id,
                    AuditDocumentCheck.audit_case_id == case.id,
                    AuditDocumentCheck.document_id == document_id,
                )
                .order_by(AuditDocumentCheck.created_at.desc(), AuditDocumentCheck.id.desc())
            ).first()
            if latest is None:
                blockers.append(_blocker("CHECK_REQUIRED", "请先对当前主文件执行文件检查。"))
                check = {"status": "missing", "stale": False, "has_errors": False}
            else:
                from app.audit_cases.workbench_checks import check_is_stale

                stale = check_is_stale(db, case, latest)
                has_errors = any(
                    finding.get("severity") == "error" for finding in (latest.findings_json or [])
                )
                check = {"status": latest.status, "stale": stale, "has_errors": has_errors}
                if latest.status != "completed":
                    blockers.append(_blocker("CHECK_NOT_COMPLETED", "当前文件检查尚未完成。"))
                elif stale:
                    blockers.append(_blocker("CHECK_STALE", "文件或规则版本已变化，请重新执行文件检查。"))
                elif has_errors:
                    blockers.append(_blocker("CHECK_ERRORS", "文件检查发现错误，需先处理阻断问题。"))

    return {
        "process_number": definition.number,
        "enabled": definition.enabled,
        "ready": not blockers,
        "blockers": blockers,
        "predecessors": predecessors,
        "required_reference_document_ids": sorted(set(required_reference_document_ids)),
        "check": check,
    }

