from __future__ import annotations

from typing import Any

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.audit_cases.schema import AuditCaseAccessDenied
from app.audit_cases.service import record_case_event
from app.db.models import (
    AuditCase,
    ProjectDataCandidate,
    ProjectDataConflict,
    ProjectDataFieldDefinition,
    ProjectDataValue,
    ProjectDataValueRevision,
    User,
    utc_now,
)
from app.project_data.fields import (
    FieldDefinition,
    get_field_definition,
    validate_candidate_request,
    validate_field_value,
)
from app.project_data.permissions import (
    ensure_project_role,
)
from app.project_data.schema import (
    ProjectDataCandidateCreate,
    ProjectDataCandidateRead,
    ProjectDataConflictError,
    ProjectDataConflictRead,
    ProjectDataNotFound,
    ProjectDataValueRead,
    ProjectFieldValidationError,
    SourceRef,
)


def _source_json(source: SourceRef, note: str | None = None) -> dict[str, Any]:
    value = source.model_dump(exclude_none=True)
    if note and note.strip():
        value["_note"] = note.strip()
    return value


def _source_ref(value: dict[str, Any] | None) -> SourceRef | None:
    if not value:
        return None
    return SourceRef(**{key: item for key, item in value.items() if not key.startswith("_")})


def _value_read(value: ProjectDataValue) -> ProjectDataValueRead:
    return ProjectDataValueRead(
        id=value.id,
        field_key=value.field_key,
        value=value.value_json,
        status=value.status,
        revision=value.revision,
        source=_source_ref(value.source_json),
        updated_by_user_id=value.updated_by_user_id,
    )


def _candidate_read(candidate: ProjectDataCandidate) -> ProjectDataCandidateRead:
    return ProjectDataCandidateRead(
        id=candidate.id,
        field_key=candidate.field_key,
        value=candidate.value_json,
        source=_source_ref(candidate.source_json),
        status=candidate.status,
        expected_revision=candidate.expected_revision,
        submitted_by_user_id=candidate.submitted_by_user_id,
        decision_reason=candidate.decision_reason,
    )


def _conflict_read(conflict: ProjectDataConflict) -> ProjectDataConflictRead:
    return ProjectDataConflictRead(
        id=conflict.id,
        field_key=conflict.field_key,
        status=conflict.status,
        current_revision=conflict.current_revision,
        candidate_ids=list(conflict.candidate_ids_json),
        resolved_candidate_id=conflict.resolved_candidate_id,
        resolution_reason=conflict.resolution_reason,
    )


class ProjectDataService:
    def __init__(self, db: Session):
        self.db = db

    def _case_for_candidate(self, candidate: ProjectDataCandidate) -> AuditCase:
        case = self.db.get(AuditCase, candidate.audit_case_id)
        if case is None or case.tenant_id != candidate.tenant_id:
            raise ProjectDataNotFound(candidate.id)
        return case

    def _field_definition(self, case: AuditCase, field_key: str) -> FieldDefinition:
        rows = self.db.exec(
            select(ProjectDataFieldDefinition).where(
                ProjectDataFieldDefinition.field_key == field_key,
                (ProjectDataFieldDefinition.tenant_id == case.tenant_id)
                | (ProjectDataFieldDefinition.tenant_id.is_(None)),
                ProjectDataFieldDefinition.status == "active",
            )
        ).all()
        tenant_rows = [row for row in rows if row.tenant_id == case.tenant_id]
        selected = tenant_rows[0] if tenant_rows else (rows[0] if rows else None)
        if selected is None:
            return get_field_definition(field_key, tenant_id=case.tenant_id)
        return FieldDefinition(
            field_key=selected.field_key,
            label=selected.label,
            value_type=selected.value_type,
            information_domain=selected.information_domain,
            scope=selected.scope,
            required=selected.required,
            editable=selected.editable,
            sync_policy=selected.sync_policy,
            validator_name=selected.validator_name,
            source_optional=selected.source_optional,
        )

    def submit_candidate(
        self,
        case: AuditCase,
        actor: User,
        request: ProjectDataCandidateCreate,
    ) -> ProjectDataCandidate:
        if case.tenant_id != actor.tenant_id:
            raise AuditCaseAccessDenied("PROJECT_ROLE_REQUIRED")
        ensure_project_role(
            self.db,
            case,
            actor,
            {"project_admin", "reviewer", "editor"},
        )
        definition = self._field_definition(case, request.field_key)
        validate_candidate_request(request, definition)
        validate_field_value(definition, request.value)
        source = request.source
        current = self._current_value(case, request.field_key)
        submission_revision = (
            request.expected_revision
            if request.expected_revision is not None
            else (current.revision if current else 0)
        )
        candidate = ProjectDataCandidate(
            tenant_id=case.tenant_id,
            audit_case_id=case.id,
            field_key=request.field_key,
            value_json=request.value,
            source_json=_source_json(source, request.note) if source is not None else {},
            status="pending",
            expected_revision=submission_revision,
            submitted_by_user_id=actor.id,
        )
        self.db.add(candidate)
        record_case_event(
            self.db,
            case=case,
            actor_user_id=actor.id,
            event_type="project_field_candidate_created",
            resource_type="project_data_candidate",
            resource_id=candidate.id,
            metadata={"status": candidate.status},
        )
        self.db.commit()
        self.db.refresh(candidate)
        return candidate

    def _load_candidate_for_actor(
        self,
        candidate_id: str,
        actor: User,
    ) -> tuple[ProjectDataCandidate, AuditCase]:
        candidate = self.db.get(ProjectDataCandidate, candidate_id)
        if candidate is None or candidate.tenant_id != actor.tenant_id:
            raise ProjectDataNotFound(candidate_id)
        case = self._case_for_candidate(candidate)
        return candidate, case

    def _current_value(self, case: AuditCase, field_key: str) -> ProjectDataValue | None:
        return self.db.exec(
            select(ProjectDataValue).where(
                ProjectDataValue.tenant_id == case.tenant_id,
                ProjectDataValue.audit_case_id == case.id,
                ProjectDataValue.field_key == field_key,
            )
        ).first()

    def _create_conflict(
        self,
        candidate: ProjectDataCandidate,
        case: AuditCase,
        current_revision: int,
        actor: User,
        reason: str,
    ) -> None:
        existing = self._open_conflict(candidate, case, current_revision)
        if existing is not None:
            raise ProjectDataConflictError(reason)

        candidate_ids = [candidate.id]
        pending = self.db.exec(
            select(ProjectDataCandidate).where(
                ProjectDataCandidate.tenant_id == case.tenant_id,
                ProjectDataCandidate.audit_case_id == case.id,
                ProjectDataCandidate.field_key == candidate.field_key,
                ProjectDataCandidate.status == "pending",
            )
        ).all()
        for other in pending:
            if other.id not in candidate_ids:
                candidate_ids.append(other.id)
        conflict = ProjectDataConflict(
            tenant_id=case.tenant_id,
            audit_case_id=case.id,
            field_key=candidate.field_key,
            status="open",
            current_revision=current_revision,
            trigger_candidate_id=candidate.id,
            candidate_ids_json=candidate_ids,
        )
        self.db.add(conflict)
        record_case_event(
            self.db,
            case=case,
            actor_user_id=actor.id,
            event_type="project_field_conflict_detected",
            resource_type="project_data_conflict",
            resource_id=conflict.id,
            metadata={"status": conflict.status},
        )
        try:
            self.db.commit()
        except IntegrityError as exc:
            self.db.rollback()
            if self._open_conflict(candidate, case, current_revision) is None:
                raise
            raise ProjectDataConflictError(reason) from exc
        raise ProjectDataConflictError(reason)

    def _open_conflict(
        self,
        candidate: ProjectDataCandidate,
        case: AuditCase,
        current_revision: int,
    ) -> ProjectDataConflict | None:
        conflicts = self.db.exec(
            select(ProjectDataConflict).where(
                ProjectDataConflict.tenant_id == case.tenant_id,
                ProjectDataConflict.audit_case_id == case.id,
                ProjectDataConflict.field_key == candidate.field_key,
                ProjectDataConflict.current_revision == current_revision,
                ProjectDataConflict.status == "open",
            )
        ).all()
        return next(
            (
                conflict
                for conflict in conflicts
                if conflict.trigger_candidate_id == candidate.id
                or candidate.id in conflict.candidate_ids_json
            ),
            None,
        )

    def _has_different_pending_candidate(
        self,
        candidate: ProjectDataCandidate,
        case: AuditCase,
    ) -> bool:
        pending = self.db.exec(
            select(ProjectDataCandidate).where(
                ProjectDataCandidate.tenant_id == case.tenant_id,
                ProjectDataCandidate.audit_case_id == case.id,
                ProjectDataCandidate.field_key == candidate.field_key,
                ProjectDataCandidate.status == "pending",
            )
        ).all()
        return any(
            other.id != candidate.id and other.value_json != candidate.value_json
            for other in pending
        )

    def _apply_candidate(
        self,
        candidate: ProjectDataCandidate,
        case: AuditCase,
        actor: User,
        *,
        reason: str | None,
        conflict: ProjectDataConflict | None = None,
    ) -> ProjectDataValue:
        expected_revision = (
            conflict.current_revision if conflict is not None else candidate.expected_revision
        )
        if expected_revision is None:
            self.db.rollback()
            raise ProjectDataConflictError("PROJECT_DATA_CONFLICT")

        current = self._current_value(case, candidate.field_key)
        next_revision = expected_revision + 1
        timestamp = utc_now()
        try:
            candidate_result = self.db.exec(
                update(ProjectDataCandidate)
                .where(
                    ProjectDataCandidate.id == candidate.id,
                    ProjectDataCandidate.tenant_id == case.tenant_id,
                    ProjectDataCandidate.audit_case_id == case.id,
                    ProjectDataCandidate.field_key == candidate.field_key,
                    ProjectDataCandidate.status == "pending",
                )
                .values(
                    status="approved",
                    decided_by_user_id=actor.id,
                    decision_reason=reason,
                    decided_at=timestamp,
                    updated_at=timestamp,
                )
                .execution_options(synchronize_session=False)
            )
            if getattr(candidate_result, "rowcount", 0) != 1:
                raise ProjectDataConflictError("PROJECT_DATA_CONFLICT")

            if conflict is not None:
                conflict_result = self.db.exec(
                    update(ProjectDataConflict)
                    .where(
                        ProjectDataConflict.id == conflict.id,
                        ProjectDataConflict.tenant_id == case.tenant_id,
                        ProjectDataConflict.audit_case_id == case.id,
                        ProjectDataConflict.field_key == candidate.field_key,
                        ProjectDataConflict.status == "open",
                        ProjectDataConflict.current_revision == expected_revision,
                    )
                    .values(
                        status="resolved",
                        resolved_candidate_id=candidate.id,
                        resolved_by_user_id=actor.id,
                        resolution_reason=reason,
                        resolved_at=timestamp,
                        updated_at=timestamp,
                    )
                    .execution_options(synchronize_session=False)
                )
                if getattr(conflict_result, "rowcount", 0) != 1:
                    raise ProjectDataConflictError("PROJECT_DATA_CONFLICT")

            if expected_revision == 0:
                current = ProjectDataValue(
                    tenant_id=case.tenant_id,
                    audit_case_id=case.id,
                    field_key=candidate.field_key,
                    value_json=candidate.value_json,
                    status="approved",
                    revision=next_revision,
                    source_json=dict(candidate.source_json),
                    approved_by_user_id=actor.id,
                    approved_at=timestamp,
                    updated_by_user_id=actor.id,
                    created_at=timestamp,
                    updated_at=timestamp,
                )
                self.db.add(current)
                self.db.flush()
            else:
                current_result = self.db.exec(
                    update(ProjectDataValue)
                    .where(
                        ProjectDataValue.tenant_id == case.tenant_id,
                        ProjectDataValue.audit_case_id == case.id,
                        ProjectDataValue.field_key == candidate.field_key,
                        ProjectDataValue.revision == expected_revision,
                    )
                    .values(
                        value_json=candidate.value_json,
                        status="approved",
                        revision=next_revision,
                        source_json=dict(candidate.source_json),
                        approved_by_user_id=actor.id,
                        approved_at=timestamp,
                        updated_by_user_id=actor.id,
                        updated_at=timestamp,
                    )
                    .execution_options(synchronize_session=False)
                )
                if getattr(current_result, "rowcount", 0) != 1 or current is None:
                    raise ProjectDataConflictError("PROJECT_DATA_CONFLICT")

            self.db.add(
                ProjectDataValueRevision(
                    tenant_id=case.tenant_id,
                    audit_case_id=case.id,
                    field_key=candidate.field_key,
                    revision=next_revision,
                    value_json=candidate.value_json,
                    status="approved",
                    source_json=dict(candidate.source_json),
                    operation="approve" if conflict is None else "resolve_conflict",
                    actor_user_id=actor.id,
                    created_at=timestamp,
                )
            )
            record_case_event(
                self.db,
                case=case,
                actor_user_id=actor.id,
                event_type=(
                    "project_field_conflict_resolved"
                    if conflict is not None
                    else "project_field_approved"
                ),
                resource_type="project_data_value",
                resource_id=current.id,
                metadata={"status": "approved", "version": next_revision},
            )
            self.db.commit()
        except ProjectDataConflictError:
            self.db.rollback()
            raise
        except IntegrityError as exc:
            self.db.rollback()
            raise ProjectDataConflictError("PROJECT_DATA_CONFLICT") from exc
        self.db.refresh(current)
        return current

    def approve_candidate(
        self,
        candidate_id: str,
        actor: User,
        expected_current_revision: int | None,
        reason: str | None,
    ) -> ProjectDataValue:
        candidate, case = self._load_candidate_for_actor(candidate_id, actor)
        ensure_project_role(self.db, case, actor, {"project_admin", "reviewer"})
        if candidate.status != "pending":
            raise ProjectDataConflictError("CANDIDATE_NOT_PENDING")
        current = self._current_value(case, candidate.field_key)
        current_revision = current.revision if current else 0
        baseline_mismatch = (
            candidate.expected_revision is None
            or candidate.expected_revision != current_revision
            or (
                expected_current_revision is not None
                and expected_current_revision != candidate.expected_revision
            )
        )
        if baseline_mismatch or self._has_different_pending_candidate(candidate, case):
            self._create_conflict(
                candidate,
                case,
                current_revision,
                actor,
                "PROJECT_DATA_CONFLICT",
            )
        open_conflict = self._open_conflict(candidate, case, current_revision)
        return self._apply_candidate(
            candidate,
            case,
            actor,
            reason=reason,
            conflict=open_conflict,
        )

    def reject_candidate(
        self,
        candidate_id: str,
        actor: User,
        reason: str,
    ) -> ProjectDataCandidate:
        candidate, case = self._load_candidate_for_actor(candidate_id, actor)
        ensure_project_role(self.db, case, actor, {"project_admin", "reviewer"})
        normalized_reason = reason.strip()
        if not normalized_reason:
            raise ProjectFieldValidationError("REJECTION_REASON_REQUIRED")
        if candidate.status != "pending":
            raise ProjectDataConflictError("CANDIDATE_NOT_PENDING")
        timestamp = utc_now()
        try:
            candidate_result = self.db.exec(
                update(ProjectDataCandidate)
                .where(
                    ProjectDataCandidate.id == candidate.id,
                    ProjectDataCandidate.tenant_id == case.tenant_id,
                    ProjectDataCandidate.audit_case_id == case.id,
                    ProjectDataCandidate.field_key == candidate.field_key,
                    ProjectDataCandidate.status == "pending",
                )
                .values(
                    status="rejected",
                    decided_by_user_id=actor.id,
                    decision_reason=normalized_reason,
                    decided_at=timestamp,
                    updated_at=timestamp,
                )
                .execution_options(synchronize_session=False)
            )
            if getattr(candidate_result, "rowcount", 0) != 1:
                raise ProjectDataConflictError("PROJECT_DATA_CONFLICT")
            record_case_event(
                self.db,
                case=case,
                actor_user_id=actor.id,
                event_type="project_field_candidate_rejected",
                resource_type="project_data_candidate",
                resource_id=candidate.id,
                metadata={"status": "rejected"},
            )
            self.db.commit()
        except ProjectDataConflictError:
            self.db.rollback()
            raise
        self.db.refresh(candidate)
        return candidate

    def list_fields(self, case: AuditCase, actor: User) -> list[ProjectDataValueRead]:
        ensure_project_role(self.db, case, actor, {"project_admin", "reviewer", "editor", "viewer"})
        values = self.db.exec(
            select(ProjectDataValue).where(
                ProjectDataValue.tenant_id == case.tenant_id,
                ProjectDataValue.audit_case_id == case.id,
            ).order_by(ProjectDataValue.field_key)
        ).all()
        return [_value_read(value) for value in values]

    def list_conflicts(self, case: AuditCase, actor: User) -> list[ProjectDataConflictRead]:
        ensure_project_role(self.db, case, actor, {"project_admin", "reviewer", "editor", "viewer"})
        conflicts = self.db.exec(
            select(ProjectDataConflict).where(
                ProjectDataConflict.tenant_id == case.tenant_id,
                ProjectDataConflict.audit_case_id == case.id,
            ).order_by(ProjectDataConflict.created_at)
        ).all()
        return [_conflict_read(conflict) for conflict in conflicts]

    def resolve_conflict(
        self,
        conflict_id: str,
        selected_candidate_id: str,
        actor: User,
        reason: str,
    ) -> ProjectDataValue:
        conflict = self.db.get(ProjectDataConflict, conflict_id)
        if conflict is None or conflict.tenant_id != actor.tenant_id:
            raise ProjectDataNotFound(conflict_id)
        case = self.db.get(AuditCase, conflict.audit_case_id)
        if case is None or case.tenant_id != conflict.tenant_id:
            raise ProjectDataNotFound(conflict_id)
        ensure_project_role(self.db, case, actor, {"project_admin", "reviewer"})
        normalized_reason = reason.strip()
        if not normalized_reason:
            raise ProjectFieldValidationError("RESOLUTION_REASON_REQUIRED")
        if conflict.status != "open" or selected_candidate_id not in conflict.candidate_ids_json:
            raise ProjectDataConflictError("PROJECT_DATA_CONFLICT")
        candidate = self.db.get(ProjectDataCandidate, selected_candidate_id)
        if (
            candidate is None
            or candidate.tenant_id != case.tenant_id
            or candidate.audit_case_id != case.id
            or candidate.field_key != conflict.field_key
            or candidate.status != "pending"
        ):
            raise ProjectDataConflictError("PROJECT_DATA_CONFLICT")
        current = self._current_value(case, conflict.field_key)
        current_revision = current.revision if current else 0
        if current_revision != conflict.current_revision:
            raise ProjectDataConflictError("PROJECT_DATA_CONFLICT")
        return self._apply_candidate(
            candidate,
            case,
            actor,
            reason=normalized_reason,
            conflict=conflict,
        )
