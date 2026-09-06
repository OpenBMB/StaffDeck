"""Durable, deterministic checks against immutable document/rule versions.

Unstructured semantic assertions are explicitly left for human review. No
model output or parser guess is treated as confirmed certification evidence.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from sqlalchemy import update
from sqlmodel import Session, select

from app.async_jobs import enqueue_async_job
from app.audit_cases.schema import AuditCaseAccessDenied
from app.db.models import (
    AuditCase,
    AuditCaseDocument,
    AuditCaseDocumentVersion,
    ProjectDataFieldDefinition,
    ProjectRuleBinding,
    RuleDefinition,
    User,
    utc_now,
)
from app.db.workbench_checks import AuditDocumentCheck
from app.project_data.permissions import ensure_project_role, resolve_project_role
from app.project_data.schema import RuleEvaluationContext
from app.rules.schema import RuleDefinitionCreate
from app.rules.service import evaluate_rule_without_model


class DocumentCheckError(ValueError):
    pass


def _reserve_case(db: Session, case: AuditCase) -> None:
    db.exec(
        update(AuditCase)
        .where(AuditCase.id == case.id, AuditCase.tenant_id == case.tenant_id)
        .values(updated_at=AuditCase.updated_at)
        .execution_options(synchronize_session=False)
    )
    db.expire_all()


def _version(db: Session, case: AuditCase, document_id: str, version_id: str | None = None):
    doc = db.exec(
        select(AuditCaseDocument)
        .where(
            AuditCaseDocument.id == document_id,
            AuditCaseDocument.tenant_id == case.tenant_id,
            AuditCaseDocument.audit_case_id == case.id,
        )
        .execution_options(populate_existing=True)
    ).first()
    if doc is None:
        raise DocumentCheckError("CHECK_DOCUMENT_NOT_FOUND")
    version = db.exec(
        select(AuditCaseDocumentVersion).where(
            AuditCaseDocumentVersion.id == (version_id or doc.active_version_id),
            AuditCaseDocumentVersion.document_id == doc.id,
            AuditCaseDocumentVersion.tenant_id == case.tenant_id,
            AuditCaseDocumentVersion.audit_case_id == case.id,
        )
    ).first()
    if version is None:
        raise DocumentCheckError("CHECK_VERSION_NOT_FOUND")
    return doc, version


def _binding_versions(db: Session, case: AuditCase, doc_id: str, version_id: str) -> list[str]:
    return sorted(
        set(
            db.exec(
                select(ProjectRuleBinding.rule_set_version_id).where(
                    ProjectRuleBinding.tenant_id == case.tenant_id,
                    ProjectRuleBinding.audit_case_id == case.id,
                    ProjectRuleBinding.document_id == doc_id,
                    ProjectRuleBinding.document_version_id == version_id,
                    ProjectRuleBinding.status == "current",
                )
            ).all()
        )
    )


def check_is_stale(db: Session, case: AuditCase, job: AuditDocumentCheck) -> bool:
    for ref in [
        {"document_id": job.document_id, "document_version_id": job.document_version_id},
        *job.reference_versions_json,
    ]:
        try:
            doc, _ = _version(db, case, ref["document_id"], ref["document_version_id"])
        except DocumentCheckError:
            return True
        if doc.active_version_id != ref["document_version_id"]:
            return True
    snapshot_versions = sorted({entry["rule_set_version_id"] for entry in job.rule_snapshot_json})
    return snapshot_versions != _binding_versions(
        db, case, job.document_id, job.document_version_id
    )


def check_read(db: Session, case: AuditCase, job: AuditDocumentCheck) -> dict[str, Any]:
    return {
        "id": job.id,
        "document_id": job.document_id,
        "document_version_id": job.document_version_id,
        "status": job.status,
        "generation": job.generation,
        "reference_versions": job.reference_versions_json,
        "rule_snapshot": job.rule_snapshot_json,
        "findings": job.findings_json,
        "error_code": job.error_code,
        "stale": check_is_stale(db, case, job),
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }


def _rules_snapshot(db: Session, case: AuditCase, doc_id: str, version_id: str):
    result = []
    for ruleset_version in _binding_versions(db, case, doc_id, version_id):
        rules = db.exec(
            select(RuleDefinition)
            .where(
                RuleDefinition.tenant_id == case.tenant_id,
                RuleDefinition.rule_set_version_id == ruleset_version,
                RuleDefinition.enabled.is_(True),
            )
            .order_by(RuleDefinition.sequence, RuleDefinition.rule_key)
        ).all()
        # Keep empty published bindings too, so staleness does not depend on rule count.
        entries = []
        for rule in rules:
            entries.append(
                {
                    "rule_key": rule.rule_key,
                    "name": rule.name,
                    "description": rule.description,
                    "execution_level": rule.execution_level,
                    "execution_method": rule.execution_method,
                    "condition": rule.condition_json,
                    "field_keys": rule.field_keys_json,
                    "source_refs": rule.source_refs_json,
                }
            )
        result.append({"rule_set_version_id": ruleset_version, "rules": entries})
    return result


def create_check(
    db: Session,
    case: AuditCase,
    actor: User,
    document_id: str,
    reference_document_ids: list[str],
    request_key: str,
) -> AuditDocumentCheck:
    ensure_project_role(db, case, actor, {"project_admin", "reviewer", "editor"})
    if case.status == "archived":
        raise DocumentCheckError("CHECK_CASE_ARCHIVED")
    _reserve_case(db, case)
    ensure_project_role(db, case, actor, {"project_admin", "reviewer", "editor"})
    ref_ids = sorted(set(reference_document_ids) - {document_id})
    payload_hash = hashlib.sha256(json.dumps([document_id, ref_ids, actor.id]).encode()).hexdigest()
    existing = db.exec(
        select(AuditDocumentCheck).where(
            AuditDocumentCheck.tenant_id == case.tenant_id,
            AuditDocumentCheck.audit_case_id == case.id,
            AuditDocumentCheck.request_key == request_key,
        )
    ).first()
    if existing:
        if existing.request_hash != payload_hash:
            raise DocumentCheckError("CHECK_REQUEST_KEY_REUSED")
        db.commit()
        return existing
    doc, version = _version(db, case, document_id)
    if doc.status == "archived":
        raise DocumentCheckError("CHECK_DOCUMENT_ARCHIVED")
    references = []
    for ref_id in ref_ids:
        ref_doc, ref_version = _version(db, case, ref_id)
        references.append({"document_id": ref_doc.id, "document_version_id": ref_version.id})
    labels = db.exec(
        select(ProjectDataFieldDefinition).where(
            (ProjectDataFieldDefinition.tenant_id == case.tenant_id)
            | ProjectDataFieldDefinition.tenant_id.is_(None),
            ProjectDataFieldDefinition.status == "active",
        )
    ).all()
    field_labels: dict[str, str] = {}
    for row in labels:
        # Tenant-specific definitions intentionally override system defaults.
        if row.tenant_id is None or row.field_key not in field_labels:
            field_labels[row.field_key] = row.label
    job = AuditDocumentCheck(
        tenant_id=case.tenant_id,
        audit_case_id=case.id,
        document_id=doc.id,
        document_version_id=version.id,
        created_by_user_id=actor.id,
        request_key=request_key,
        request_hash=payload_hash,
        reference_versions_json=references,
        rule_snapshot_json=_rules_snapshot(db, case, doc.id, version.id),
        field_labels_json=field_labels,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def _labelled_fields(content: str) -> dict[str, list[tuple[str, str]]]:
    fields: dict[str, list[tuple[str, str]]] = {}
    for line in content.splitlines():
        if line.strip().startswith("|"):
            cells = [part.strip().strip("*") for part in line.strip().strip("|").split("|")]
            pair = cells[:2] if len(cells) == 2 else []
        else:
            match = re.match(
                r"^\s*(?:[-*]\s+)?\*{0,2}([^:：|]{1,100}?)\*{0,2}\s*[:：]\s*(.*)$", line
            )
            pair = [match[1].strip().strip("*"), match[2].strip().strip("*")] if match else []
        if len(pair) == 2 and pair[0] and pair[1] and not set(pair[0]) <= {"-", ":", " "}:
            fields.setdefault(pair[0], []).append((pair[1], line[:500]))
    return fields


def evaluate_document_check(db: Session, case: AuditCase, job: AuditDocumentCheck):
    _, version = _version(db, case, job.document_id, job.document_version_id)
    fields = _labelled_fields(version.content)
    findings = []

    def add(code, severity, title, detail, excerpt="", ref=None):
        findings.append(
            {
                "code": code,
                "severity": severity,
                "title": title,
                "detail": detail,
                "document_id": job.document_id,
                "document_version_id": version.id,
                "evidence_excerpt": excerpt,
                **({"reference": ref} if ref else {}),
            }
        )

    if not version.content.strip():
        add("DOCUMENT_EMPTY", "error", "文件内容为空", "请填写当前文件后重新检查。")
    if not job.rule_snapshot_json:
        add(
            "RULES_UNCONFIGURED",
            "warning",
            "尚未绑定本版本的文件规则",
            "本次仅执行内容与关联字段检查，不能视为文件规则全部通过。",
        )
    for binding in job.rule_snapshot_json:
        for raw_rule in binding["rules"]:
            rule = RuleDefinitionCreate.model_validate(raw_rule)
            key = rule.condition.get("field_key")
            label = job.field_labels_json.get(key, key)
            matches = fields.get(key, fields.get(label, [])) if key else []
            if (
                rule.execution_method == "model_assisted"
                or len(matches) != 1
                or rule.condition.get("operator") == "has_evidence"
            ):
                add(
                    "RULE_REQUIRES_REVIEW",
                    "warning",
                    rule.name,
                    "该规则需要人工语义/证据复核，或无法从明确标注字段唯一提取输入；未判定通过。",
                    "\n".join(value[1] for value in matches)[:1000],
                )
                continue
            value: Any = matches[0][0]
            expected = rule.condition.get("value")
            if isinstance(expected, (int, float)) and not isinstance(expected, bool):
                try:
                    value = float(value)
                except ValueError:
                    add(
                        "RULE_INPUT_UNREADABLE",
                        "warning",
                        rule.name,
                        "字段无法解析为数值，需要人工确认。",
                        matches[0][1],
                    )
                    continue
            result = evaluate_rule_without_model(
                rule, RuleEvaluationContext(project_fields={key: value})
            )
            add(
                "RULE_" + result.status.upper(),
                "error"
                if result.blocking
                else ("info" if result.status == "passed" else "warning"),
                rule.name,
                f"规则 {rule.rule_key}；依据明确标注字段“{label}”执行。",
                matches[0][1],
            )
    comparable = ("企业名称", "组织名称", "认证范围", "审核范围", "统一社会信用代码", "审核日期")
    for reference in job.reference_versions_json:
        ref_doc, ref_version = _version(
            db, case, reference["document_id"], reference["document_version_id"]
        )
        other_fields = _labelled_fields(ref_version.content)
        compared = 0
        for key in comparable:
            left, right = fields.get(key, []), other_fields.get(key, [])
            if len(left) != 1 or len(right) != 1:
                continue
            compared += 1
            same = left[0][0] == right[0][0]
            add(
                "CROSS_FIELD_MATCH" if same else "CROSS_FIELD_MISMATCH",
                "info" if same else "warning",
                f"{key}与《{ref_doc.title}》" + ("一致" if same else "存在差异"),
                "比较两份文件中明确标注的同名字段，差异需人工核实适用范围。",
                f"当前文件：{left[0][1]}\n关联文件：{right[0][1]}",
                reference,
            )
        if not compared:
            add(
                "CROSS_FIELDS_UNAVAILABLE",
                "warning",
                f"《{ref_doc.title}》需要人工交叉核查",
                "未找到双方均唯一标注的可比字段，本次未完成该关联文件的一致性判断。",
                ref=reference,
            )
    if not any(f["severity"] == "error" for f in findings):
        add(
            "CHECK_SCOPE",
            "info",
            "已完成本次可执行检查",
            "检查范围为文件内容、明确标注字段及绑定的确定性规则；警告事项仍需人工复核。",
        )
    return findings


def run_document_check(engine, job_id: str, generation: int) -> None:
    with Session(engine) as db:
        claimed = db.exec(
            update(AuditDocumentCheck)
            .where(
                AuditDocumentCheck.id == job_id,
                AuditDocumentCheck.status == "queued",
                AuditDocumentCheck.generation == generation,
            )
            .values(status="running", updated_at=utc_now())
        )
        db.commit()
        if claimed.rowcount != 1:
            return
        job = db.get(AuditDocumentCheck, job_id)
        try:
            case = db.get(AuditCase, job.audit_case_id)
            actor = db.get(User, job.created_by_user_id)
            if (
                case is None
                or actor is None
                or case.tenant_id != job.tenant_id
                or resolve_project_role(db, case, actor)
                not in {"editor", "reviewer", "project_admin"}
            ):
                raise AuditCaseAccessDenied("CHECK_ACCESS_REVOKED")
            if case.status == "archived":
                raise DocumentCheckError("CHECK_CASE_ARCHIVED")
            findings = evaluate_document_check(db, case, job)
            db.exec(
                update(AuditDocumentCheck)
                .where(
                    AuditDocumentCheck.id == job_id,
                    AuditDocumentCheck.status == "running",
                    AuditDocumentCheck.generation == generation,
                )
                .values(
                    status="completed",
                    findings_json=findings,
                    error_code=None,
                    updated_at=utc_now(),
                )
            )
            db.commit()
        except Exception:
            db.rollback()
            # Do not return exception strings that may contain file content or provider secrets.
            db.exec(
                update(AuditDocumentCheck)
                .where(
                    AuditDocumentCheck.id == job_id,
                    AuditDocumentCheck.status == "running",
                    AuditDocumentCheck.generation == generation,
                )
                .values(status="failed", error_code="CHECK_EXECUTION_FAILED", updated_at=utc_now())
            )
            db.commit()


def enqueue_check(engine, job: AuditDocumentCheck) -> None:
    if job.status != "queued":
        return
    try:
        enqueue_async_job(
            "audit_document_check", run_document_check, engine, job.id, job.generation
        )
    except Exception:
        with Session(engine) as db:
            db.exec(
                update(AuditDocumentCheck)
                .where(
                    AuditDocumentCheck.id == job.id,
                    AuditDocumentCheck.status == "queued",
                    AuditDocumentCheck.generation == job.generation,
                )
                .values(status="failed", error_code="CHECK_QUEUE_UNAVAILABLE", updated_at=utc_now())
            )
            db.commit()


def recover_document_checks(engine) -> None:
    """Called once on startup while the app's exclusive runtime lock is held."""
    with Session(engine) as db:
        db.exec(
            update(AuditDocumentCheck)
            .where(
                AuditDocumentCheck.status.in_(["queued", "running"]),
            )
            .values(status="failed", error_code="CHECK_INTERRUPTED", updated_at=utc_now())
        )
        db.commit()


def retry_check(db: Session, case: AuditCase, actor: User, job_id: str, request_key: str):
    ensure_project_role(db, case, actor, {"editor", "reviewer", "project_admin"})
    if case.status == "archived":
        raise DocumentCheckError("CHECK_CASE_ARCHIVED")
    _reserve_case(db, case)
    ensure_project_role(db, case, actor, {"editor", "reviewer", "project_admin"})
    job = db.exec(
        select(AuditDocumentCheck)
        .where(
            AuditDocumentCheck.id == job_id,
            AuditDocumentCheck.tenant_id == case.tenant_id,
            AuditDocumentCheck.audit_case_id == case.id,
        )
        .execution_options(populate_existing=True)
    ).first()
    if job is None:
        raise DocumentCheckError("CHECK_NOT_FOUND")
    if request_key in job.retry_keys_json:
        db.commit()
        return job
    if job.status != "failed":
        raise DocumentCheckError("CHECK_RETRY_REQUIRES_FAILURE")
    if check_is_stale(db, case, job):
        raise DocumentCheckError("CHECK_STALE_CREATE_NEW")
    doc, _ = _version(db, case, job.document_id)
    if doc.status == "archived":
        raise DocumentCheckError("CHECK_DOCUMENT_ARCHIVED")
    job.status = "queued"
    job.generation += 1
    job.created_by_user_id = actor.id
    job.error_code = None
    job.updated_at = utc_now()
    job.retry_keys_json = [*job.retry_keys_json, request_key]
    db.add(job)
    db.commit()
    db.refresh(job)
    return job
