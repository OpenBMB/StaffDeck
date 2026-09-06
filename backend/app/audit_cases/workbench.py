"""Version-bound internal document reviews, independent of formal certification decisions."""

from __future__ import annotations

import hashlib
import json

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from sqlalchemy import inspect, update
from sqlmodel import Session, select

from app.audit_cases.workbench_processes import (
    ENABLED_PROCESS_NUMBERS,
    all_process_definitions,
    evaluate_process_gate,
)
from app.audit_cases.workbench_schema import (
    WorkIssueCreate,
    WorkIssueTransition,
    WorkItemCreate,
    WorkItemTransition,
)
from app.db.models import (
    AuditCase,
    AuditCaseDocument,
    AuditCaseDocumentVersion,
    AuditCaseMemberRole,
    AuditWorkbenchEvent,
    AuditWorkbenchIssue,
    AuditWorkItem,
    User,
    utc_now,
)
from app.project_data.permissions import resolve_project_role

READ_ROLES = {"project_admin", "reviewer", "editor", "viewer"}
WRITE_ROLES = {"project_admin", "reviewer", "editor"}
REVIEW_ROLES = {"project_admin", "reviewer"}
# Compatibility alias kept for existing imports and callers.
PILOT_PROCESSES = set(ENABLED_PROCESS_NUMBERS)


def fail(code: str, status: int = 409):
    raise HTTPException(status_code=status, detail=code)


def begin_case_write(db: Session, case: AuditCase) -> None:
    """Reserve SQLite's writer before checking state; caller commits all changes atomically."""
    db.execute(
        update(AuditCase)
        .where(AuditCase.id == case.id, AuditCase.tenant_id == case.tenant_id)
        .values(updated_at=AuditCase.updated_at)
        .execution_options(synchronize_session=False)
    )
    db.expire_all()


def is_document_submitted(db: Session, case: AuditCase, document_id: str) -> bool:
    return (
        db.exec(
            select(AuditWorkItem.id).where(
                AuditWorkItem.tenant_id == case.tenant_id,
                AuditWorkItem.audit_case_id == case.id,
                AuditWorkItem.document_id == document_id,
                AuditWorkItem.status.in_(["submitted", "approved"]),
            )
        ).first()
        is not None
    )


class AuditWorkbenchService:
    def __init__(self, db: Session):
        self.db = db

    def role(self, case: AuditCase, actor: User) -> str | None:
        if actor.tenant_id != case.tenant_id:
            return None
        if (
            actor.role != "admin"
            and actor.id != case.owner_user_id
            and actor.id not in (case.member_user_ids_json or [])
        ):
            return None
        return resolve_project_role(self.db, case, actor)

    def authorize(self, case: AuditCase, actor: User, roles=READ_ROLES, *, write=False):
        if self.role(case, actor) not in roles:
            fail("PROJECT_ROLE_REQUIRED", 403)
        if write and case.status == "archived":
            fail("AUDIT_CASE_READ_ONLY")

    def case(self, tenant_id: str, case_id: str, actor: User) -> AuditCase:
        if actor.tenant_id != tenant_id:
            fail("TENANT_ACCESS_DENIED", 403)
        case = self.db.exec(
            select(AuditCase).where(AuditCase.id == case_id, AuditCase.tenant_id == tenant_id)
        ).first()
        if case is None:
            fail("AUDIT_CASE_NOT_FOUND", 404)
        self.authorize(case, actor)
        return case

    def _write(self, case, actor, roles=WRITE_ROLES):
        self.authorize(case, actor, roles, write=True)
        begin_case_write(self.db, case)
        self.authorize(case, actor, roles, write=True)

    def document(self, case, document_id, *, primary=False):
        doc = self.db.exec(
            select(AuditCaseDocument).where(
                AuditCaseDocument.id == document_id,
                AuditCaseDocument.tenant_id == case.tenant_id,
                AuditCaseDocument.audit_case_id == case.id,
            )
        ).first()
        if doc is None:
            fail("AUDIT_CASE_DOCUMENT_NOT_FOUND", 404)
        if primary and doc.status == "archived":
            fail("AUDIT_CASE_DOCUMENT_READ_ONLY")
        version = (
            self.db.get(AuditCaseDocumentVersion, doc.active_version_id)
            if doc.active_version_id
            else None
        )
        if (
            version is None
            or version.document_id != doc.id
            or version.audit_case_id != case.id
            or version.tenant_id != case.tenant_id
        ):
            fail("DOCUMENT_VERSION_REQUIRED")
        return doc

    def _member(self, case, user_id, roles):
        user = self.db.get(User, user_id)
        if (
            user is None
            or user.tenant_id != case.tenant_id
            or (user_id != case.owner_user_id and user_id not in (case.member_user_ids_json or []))
        ):
            fail("PROJECT_MEMBER_REQUIRED", 403)
        if self.role(case, user) not in roles:
            fail("ASSIGNMENT_ROLE_REQUIRED", 403)
        return user

    def _item(self, case, item_id):
        row = self.db.exec(
            select(AuditWorkItem).where(
                AuditWorkItem.id == item_id,
                AuditWorkItem.tenant_id == case.tenant_id,
                AuditWorkItem.audit_case_id == case.id,
            )
        ).first()
        if row is None:
            fail("WORK_ITEM_NOT_FOUND", 404)
        return row

    def _items(self, case):
        return self.db.exec(
            select(AuditWorkItem)
            .where(
                AuditWorkItem.tenant_id == case.tenant_id, AuditWorkItem.audit_case_id == case.id
            )
            .order_by(AuditWorkItem.created_at, AuditWorkItem.id)
        ).all()

    def _issues(self, case):
        return self.db.exec(
            select(AuditWorkbenchIssue)
            .where(
                AuditWorkbenchIssue.tenant_id == case.tenant_id,
                AuditWorkbenchIssue.audit_case_id == case.id,
            )
            .order_by(AuditWorkbenchIssue.created_at, AuditWorkbenchIssue.id)
        ).all()

    def item_read(self, row):
        if inspect(row).expired:
            self.db.refresh(row)
        value = row.model_dump(exclude={"tenant_id", "reference_versions_json"})
        value["reference_versions"] = list(row.reference_versions_json or [])
        refs = [
            {"document_id": row.document_id, "document_version_id": row.document_version_id},
            *value["reference_versions"],
        ]
        value["stale"] = any(
            (doc := self.db.get(AuditCaseDocument, ref["document_id"])) is None
            or doc.tenant_id != row.tenant_id
            or doc.audit_case_id != row.audit_case_id
            or doc.active_version_id != ref["document_version_id"]
            for ref in refs
        )
        return jsonable_encoder(value)

    def issue_read(self, row):
        if inspect(row).expired:
            self.db.refresh(row)
        return jsonable_encoder(row.model_dump(exclude={"tenant_id", "audit_case_id", "revision"}))

    def members(self, case, actor):
        ids = set(case.member_user_ids_json or []) | {case.owner_user_id}
        if actor.role == "admin":
            ids.add(actor.id)
        users = self.db.exec(
            select(User).where(User.tenant_id == case.tenant_id, User.id.in_(ids)).order_by(User.id)
        ).all()
        return [
            {
                "user_id": u.id,
                "display_name": u.display_name or u.username,
                "role": self.role(case, u),
            }
            for u in users
        ]

    def snapshot(self, case, actor):
        self.authorize(case, actor)
        return {
            "case_id": case.id,
            "role": self.role(case, actor),
            "members": self.members(case, actor),
            "processes": [
                {
                    "number": definition.number,
                    "name": definition.name,
                    "stage": definition.stage,
                    "enabled": definition.enabled,
                    "predecessor_numbers": list(definition.predecessor_numbers),
                    "required_reference_process_numbers": list(
                        definition.required_reference_process_numbers
                    ),
                    "requires_fresh_check": definition.requires_fresh_check,
                    "guidance": definition.guidance,
                }
                for definition in all_process_definitions()
            ],
            "work_items": [self.item_read(r) for r in self._items(case)],
            "issues": [self.issue_read(r) for r in self._issues(case)],
        }

    def process_gates(self, case, actor, document_id: str):
        self.authorize(case, actor)
        self.document(case, document_id)
        return [
            evaluate_process_gate(self.db, case, definition.number, document_id)
            for definition in all_process_definitions()
        ]

    def _event(
        self, case, actor, event_type, item_id, detail, *, key=None, digest=None, result=None
    ):
        self.db.add(
            AuditWorkbenchEvent(
                tenant_id=case.tenant_id,
                audit_case_id=case.id,
                work_item_id=item_id,
                event_type=event_type,
                actor_user_id=actor.id,
                request_key=key,
                payload_hash=digest,
                detail_json=detail,
                result_json=result or {},
            )
        )

    def _replay(self, case, actor, target, request):
        payload = {"actor": actor.id, "target": target, **request.model_dump()}
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        event = self.db.exec(
            select(AuditWorkbenchEvent).where(
                AuditWorkbenchEvent.tenant_id == case.tenant_id,
                AuditWorkbenchEvent.audit_case_id == case.id,
                AuditWorkbenchEvent.request_key == request.request_key,
            )
        ).first()
        if event is not None:
            if event.payload_hash != digest:
                fail("IDEMPOTENCY_KEY_CONFLICT")
            return digest, dict(event.result_json)
        return digest, None

    def create_item(self, case, actor, request: WorkItemCreate):
        self._write(case, actor)
        gate = evaluate_process_gate(self.db, case, request.process_number, request.document_id)
        if not gate["enabled"]:
            fail("PROCESS_NOT_ENABLED", 422)
        if any(blocker["code"] == "PROCESS_PRECONDITION_REQUIRED" for blocker in gate["blockers"]):
            fail("PROCESS_PRECONDITION_REQUIRED")
        required_references = set(gate["required_reference_document_ids"])
        if not required_references.issubset(set(request.reference_document_ids)):
            fail("PROCESS_REFERENCE_REQUIRED")
        self._member(case, request.assigned_to_user_id, WRITE_ROLES)
        self._member(case, request.reviewer_user_id, REVIEW_ROLES)
        if request.reviewer_user_id in {actor.id, request.assigned_to_user_id}:
            fail("SELF_REVIEW_FORBIDDEN", 403)
        doc = self.document(case, request.document_id, primary=True)
        refs = [
            self.document(case, doc_id)
            for doc_id in sorted(set(request.reference_document_ids))
            if doc_id != doc.id
        ]
        existing = self.db.exec(
            select(AuditWorkItem).where(
                AuditWorkItem.tenant_id == case.tenant_id,
                AuditWorkItem.audit_case_id == case.id,
                AuditWorkItem.document_id == doc.id,
                AuditWorkItem.process_number == request.process_number,
            )
        ).first()
        if existing is not None:
            self.db.commit()
            return self.item_read(existing)
        row = AuditWorkItem(
            tenant_id=case.tenant_id,
            audit_case_id=case.id,
            document_id=doc.id,
            document_version_id=doc.active_version_id,
            process_number=request.process_number,
            title=doc.title,
            assigned_to_user_id=request.assigned_to_user_id,
            reviewer_user_id=request.reviewer_user_id,
            reference_versions_json=[
                {"document_id": d.id, "document_version_id": d.active_version_id} for d in refs
            ],
        )
        self.db.add(row)
        self._event(
            case,
            actor,
            "item.created",
            row.id,
            {
                "document_id": doc.id,
                "process_number": row.process_number,
                "document_version_id": row.document_version_id,
                "reference_versions": row.reference_versions_json,
            },
        )
        self.db.commit()
        return self.item_read(row)

    def transition_item(self, case, actor, item_id, request: WorkItemTransition):
        self._write(case, actor)
        row = self._item(case, item_id)
        digest, replay = self._replay(case, actor, item_id, request)
        if replay is not None:
            self.db.commit()
            return replay
        if row.revision != request.expected_revision:
            fail("WORK_ITEM_REVISION_CONFLICT")
        doc = self.document(case, row.document_id, primary=True)
        before = row.status
        values = {"revision": row.revision + 1, "updated_at": utc_now()}
        if request.action == "submit":
            if actor.id != row.assigned_to_user_id:
                fail("WORK_ITEM_ASSIGNEE_REQUIRED", 403)
            if actor.id == row.reviewer_user_id:
                fail("SELF_REVIEW_FORBIDDEN", 403)
            self._member(case, row.reviewer_user_id, REVIEW_ROLES)
            if before not in {"draft", "changes_requested"}:
                fail("INVALID_WORK_ITEM_TRANSITION")
            gate = evaluate_process_gate(self.db, case, row.process_number, row.document_id)
            if any(
                blocker["code"] == "PROCESS_PRECONDITION_REQUIRED"
                for blocker in gate["blockers"]
            ):
                fail("PROCESS_PRECONDITION_REQUIRED")
            if not set(gate["required_reference_document_ids"]).issubset(
                {ref["document_id"] for ref in row.reference_versions_json}
            ):
                fail("PROCESS_REFERENCE_REQUIRED")
            refs = [self.document(case, ref["document_id"]) for ref in row.reference_versions_json]
            values.update(
                status="submitted",
                submitted_by_user_id=actor.id,
                document_version_id=doc.active_version_id,
                reference_versions_json=[
                    {"document_id": d.id, "document_version_id": d.active_version_id} for d in refs
                ],
            )
        else:
            self.authorize(case, actor, REVIEW_ROLES)
            if actor.id != row.reviewer_user_id:
                fail("ASSIGNED_REVIEWER_REQUIRED", 403)
            if actor.id == row.submitted_by_user_id:
                fail("SELF_REVIEW_FORBIDDEN", 403)
            if request.action in {"approve", "request_changes"} and before != "submitted":
                fail("INVALID_WORK_ITEM_TRANSITION")
            if request.action == "approve":
                gate = evaluate_process_gate(self.db, case, row.process_number, row.document_id)
                if any(
                    blocker["code"] == "PROCESS_PRECONDITION_REQUIRED"
                    for blocker in gate["blockers"]
                ):
                    fail("PROCESS_PRECONDITION_REQUIRED")
                if not set(gate["required_reference_document_ids"]).issubset(
                    {ref["document_id"] for ref in row.reference_versions_json}
                ):
                    fail("PROCESS_REFERENCE_REQUIRED")
                if any(blocker["code"].startswith("CHECK_") for blocker in gate["blockers"]):
                    fail("PROCESS_CHECK_REQUIRED")
                if self.item_read(row)["stale"]:
                    fail("WORK_ITEM_VERSION_STALE")
                from app.audit_cases.workbench_checks import check_is_stale
                from app.db.workbench_checks import AuditDocumentCheck

                latest_check = self.db.exec(
                    select(AuditDocumentCheck)
                    .where(
                        AuditDocumentCheck.tenant_id == case.tenant_id,
                        AuditDocumentCheck.audit_case_id == case.id,
                        AuditDocumentCheck.document_id == row.document_id,
                    )
                    .order_by(AuditDocumentCheck.created_at.desc(), AuditDocumentCheck.id.desc())
                ).first()
                if latest_check and (
                    latest_check.status != "completed"
                    or check_is_stale(self.db, case, latest_check)
                    or any(f.get("severity") == "error" for f in latest_check.findings_json)
                ):
                    fail("DOCUMENT_CHECK_BLOCKING")
                if any(
                    i.work_item_id == row.id and i.blocking and i.status != "closed"
                    for i in self._issues(case)
                ):
                    fail("BLOCKING_ISSUES_OPEN")
                values.update(status="approved", approved_version_id=row.document_version_id)
            elif request.action == "request_changes":
                values.update(status="changes_requested")
            elif request.action == "reopen":
                if before != "approved":
                    fail("INVALID_WORK_ITEM_TRANSITION")
                values.update(status="draft")
        result = self.db.execute(
            update(AuditWorkItem)
            .where(AuditWorkItem.id == row.id, AuditWorkItem.revision == request.expected_revision)
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            fail("WORK_ITEM_REVISION_CONFLICT")
        self.db.refresh(row)
        output = self.item_read(row)
        self._event(
            case,
            actor,
            f"item.{request.action}",
            row.id,
            {
                "from_status": before,
                "status": row.status,
                "revision": row.revision,
                "document_version_id": row.document_version_id,
                "approved_version_id": row.approved_version_id,
                "reference_versions": row.reference_versions_json,
                "comment": request.comment or "",
            },
            key=request.request_key,
            digest=digest,
            result=output,
        )
        self.db.commit()
        return output

    def create_issue(self, case, actor, request: WorkIssueCreate):
        self._write(case, actor, WRITE_ROLES if request.kind == "document_check" else REVIEW_ROLES)
        row = self._item(case, request.work_item_id)
        doc = self.document(case, row.document_id, primary=True)
        assigned = request.assigned_to_user_id or row.assigned_to_user_id
        self._member(case, assigned, WRITE_ROLES)
        issue = AuditWorkbenchIssue(
            tenant_id=case.tenant_id,
            audit_case_id=case.id,
            work_item_id=row.id,
            document_id=doc.id,
            document_version_id=doc.active_version_id,
            kind=request.kind,
            title=request.title,
            detail=request.detail,
            blocking=request.blocking,
            assigned_to_user_id=assigned,
            created_by_user_id=actor.id,
        )
        self.db.add(issue)
        self._event(
            case,
            actor,
            "issue.created",
            row.id,
            {
                "issue_id": issue.id,
                "kind": issue.kind,
                "blocking": issue.blocking,
                "document_version_id": issue.document_version_id,
            },
        )
        self.db.commit()
        return self.issue_read(issue)

    def transition_issue(self, case, actor, issue_id, request: WorkIssueTransition):
        self._write(case, actor)
        issue = self.db.exec(
            select(AuditWorkbenchIssue).where(
                AuditWorkbenchIssue.id == issue_id,
                AuditWorkbenchIssue.tenant_id == case.tenant_id,
                AuditWorkbenchIssue.audit_case_id == case.id,
            )
        ).first()
        if issue is None:
            fail("WORK_ISSUE_NOT_FOUND", 404)
        digest, replay = self._replay(case, actor, issue_id, request)
        if replay is not None:
            self.db.commit()
            return replay
        doc = self.document(case, issue.document_id, primary=True)
        before = issue.status
        if request.action == "respond":
            if actor.id != issue.assigned_to_user_id:
                fail("ISSUE_ASSIGNEE_REQUIRED", 403)
            if issue.status == "closed" or not request.response:
                fail("ISSUE_RESPONSE_REQUIRED")
            issue.response = request.response
            issue.response_version_id = doc.active_version_id
            issue.status = "responded"
        else:
            self.authorize(case, actor, REVIEW_ROLES)
            if actor.id == issue.assigned_to_user_id:
                fail("SELF_REVIEW_FORBIDDEN", 403)
            if request.action == "close":
                if issue.status != "responded":
                    fail("ISSUE_RESPONSE_REQUIRED")
                if issue.response_version_id != doc.active_version_id:
                    fail("ISSUE_RESPONSE_VERSION_STALE")
                issue.status = "closed"
            else:
                if issue.status != "closed":
                    fail("INVALID_ISSUE_TRANSITION")
                issue.status = "open"
        issue.revision += 1
        issue.updated_at = utc_now()
        self.db.add(issue)
        output = self.issue_read(issue)
        self._event(
            case,
            actor,
            f"issue.{request.action}",
            issue.work_item_id,
            {
                "issue_id": issue.id,
                "from_status": before,
                "status": issue.status,
                "response_version_id": issue.response_version_id,
                "response": request.response or "",
            },
            key=request.request_key,
            digest=digest,
            result=output,
        )
        self.db.commit()
        return output

    def set_member_role(self, case, actor, user_id, role):
        self._write(case, actor, {"project_admin"})
        target = self._member(case, user_id, READ_ROLES)
        if user_id == case.owner_user_id and role != "project_admin":
            fail("OWNER_ROLE_REQUIRED")
        if target.role == "admin" and role != "project_admin":
            fail("TENANT_ADMIN_ROLE_REQUIRED")
        for item in self._items(case):
            if item.status != "approved" and (
                (item.reviewer_user_id == user_id and role not in REVIEW_ROLES)
                or (item.assigned_to_user_id == user_id and role not in WRITE_ROLES)
            ):
                fail("PENDING_ASSIGNMENT_ROLE_REQUIRED")
        if role not in WRITE_ROLES and any(
            i.assigned_to_user_id == user_id and i.status != "closed" for i in self._issues(case)
        ):
            fail("PENDING_ASSIGNMENT_ROLE_REQUIRED")
        record = self.db.exec(
            select(AuditCaseMemberRole).where(
                AuditCaseMemberRole.tenant_id == case.tenant_id,
                AuditCaseMemberRole.audit_case_id == case.id,
                AuditCaseMemberRole.user_id == user_id,
            )
        ).first()
        before = self.role(case, target)
        if record is None:
            record = AuditCaseMemberRole(
                tenant_id=case.tenant_id,
                audit_case_id=case.id,
                user_id=user_id,
                created_by_user_id=actor.id,
            )
        record.role = role
        record.updated_at = utc_now()
        self.db.add(record)
        self._event(
            case,
            actor,
            "member.role_changed",
            None,
            {"user_id": user_id, "from_role": before, "role": role},
        )
        self.db.commit()
        return {
            "user_id": user_id,
            "display_name": target.display_name or target.username,
            "role": self.role(case, target),
        }

    def report_work_document(self, case, actor, report_id):
        from app.audit_cases.documents import AuditCaseDocumentService
        from app.db.models import AuditReportSection, AuditReportVersion

        self._write(case, actor)
        report = self.db.exec(
            select(AuditReportVersion).where(
                AuditReportVersion.id == report_id,
                AuditReportVersion.tenant_id == case.tenant_id,
                AuditReportVersion.audit_case_id == case.id,
            )
        ).first()
        if report is None:
            fail("AUDIT_REPORT_NOT_FOUND", 404)
        key = f"report-workdoc:{report_id}"
        previous = self.db.exec(
            select(AuditWorkbenchEvent).where(
                AuditWorkbenchEvent.tenant_id == case.tenant_id,
                AuditWorkbenchEvent.audit_case_id == case.id,
                AuditWorkbenchEvent.request_key == key,
            )
        ).first()
        if previous:
            if previous.event_type != "report.work_document_created":
                fail("IDEMPOTENCY_KEY_CONFLICT")
            return self.document(case, previous.detail_json["document_id"])
        sections = self.db.exec(
            select(AuditReportSection)
            .where(
                AuditReportSection.tenant_id == case.tenant_id,
                AuditReportSection.audit_case_id == case.id,
                AuditReportSection.report_version_id == report.id,
            )
            .order_by(AuditReportSection.sequence)
        ).all()
        if not sections or any(
            s.status != "succeeded" or not s.draft_markdown.strip() for s in sections
        ):
            fail("AUDIT_REPORT_INCOMPLETE")
        document_key = f"report-{report.id}"
        if self.db.exec(
            select(AuditCaseDocument.id).where(
                AuditCaseDocument.tenant_id == case.tenant_id,
                AuditCaseDocument.audit_case_id == case.id,
                AuditCaseDocument.document_key == document_key,
            )
        ).first():
            fail("DOCUMENT_KEY_CONFLICT")
        trace = {
            "report_id": report.id,
            "report_version": report.version,
            "source_document_id": report.source_document_id,
            "source_document_version_id": report.source_document_version_id,
            "rule_set_version_ids": report.rule_set_version_ids_json,
            "material_version_ids": report.material_version_ids_json,
            "knowledge_base_version_ids": report.knowledge_base_version_ids_json,
            "citation_ids": sorted({c for s in sections for c in s.citation_ids_json}),
        }
        content = "# 审核报告（内部复核稿）\n\n" + "\n\n".join(
            f"## {s.title}\n\n{s.draft_markdown}" for s in sections
        )
        content += "\n\n## 来源追溯快照\n\n本文件不代表正式认证决定或签发。\n\n```json\n"
        content += json.dumps(trace, ensure_ascii=False, indent=2) + "\n```\n"
        doc = AuditCaseDocument(
            tenant_id=case.tenant_id,
            audit_case_id=case.id,
            document_key=document_key,
            title=f"审核报告 v{report.version}（复核稿）",
            document_type="audit_report",
            zone="report",
            created_by_user_id=actor.id,
            updated_by_user_id=actor.id,
        )
        version = AuditCaseDocumentService._new_version(
            case,
            doc.id,
            1,
            "markdown",
            content,
            "从已生成报告创建独立复核文件；保留来源版本",
            actor,
        )
        doc.active_version_id = version.id
        self.db.add_all([doc, version])
        self._event(
            case,
            actor,
            "report.work_document_created",
            None,
            {**trace, "document_id": doc.id, "document_version_id": version.id},
            key=key,
        )
        self.db.commit()
        self.db.refresh(doc)
        return doc

    def events(self, case, actor, work_item_id=None):
        self.authorize(case, actor)
        query = select(AuditWorkbenchEvent).where(
            AuditWorkbenchEvent.tenant_id == case.tenant_id,
            AuditWorkbenchEvent.audit_case_id == case.id,
        )
        if work_item_id:
            self._item(case, work_item_id)
            query = query.where(AuditWorkbenchEvent.work_item_id == work_item_id)
        return [
            {
                "id": e.id,
                "work_item_id": e.work_item_id,
                "event_type": e.event_type,
                "actor_user_id": e.actor_user_id,
                "detail": e.detail_json,
                "created_at": e.created_at,
            }
            for e in self.db.exec(
                query.order_by(AuditWorkbenchEvent.created_at, AuditWorkbenchEvent.id)
            ).all()
        ]

    def inbox(self, tenant_id, actor):
        if actor.tenant_id != tenant_id:
            fail("TENANT_ACCESS_DENIED", 403)
        items, issues = [], []
        cases = self.db.exec(
            select(AuditCase).where(
                AuditCase.tenant_id == tenant_id, AuditCase.status != "archived"
            )
        ).all()
        for case in cases:
            role = self.role(case, actor)
            if role not in WRITE_ROLES:
                continue
            for row in self._items(case):
                reason = (
                    "submit"
                    if row.status in {"draft", "changes_requested"}
                    and row.assigned_to_user_id == actor.id
                    else "review"
                    if row.status == "submitted"
                    and row.reviewer_user_id == actor.id
                    and role in REVIEW_ROLES
                    and row.submitted_by_user_id != actor.id
                    else None
                )
                if reason:
                    items.append(
                        {
                            **self.item_read(row),
                            "organization_name": case.organization_name,
                            "task_reason": reason,
                        }
                    )
            item_map = {item.id: item for item in self._items(case)}
            for issue in self._issues(case):
                task = item_map.get(issue.work_item_id)
                reason = None
                if issue.status == "open" and issue.assigned_to_user_id == actor.id:
                    reason = "respond"
                elif (
                    issue.status == "responded"
                    and task
                    and task.reviewer_user_id == actor.id
                    and role in REVIEW_ROLES
                ):
                    reason = "verify"
                if reason:
                    issues.append(
                        {
                            **self.issue_read(issue),
                            "audit_case_id": case.id,
                            "organization_name": case.organization_name,
                            "task_reason": reason,
                        }
                    )
        return {"items": items, "issues": issues}
