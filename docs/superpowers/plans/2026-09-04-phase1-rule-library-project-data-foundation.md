# Phase 1 Rule Library and Project Data Foundation Implementation Plan

> For agentic workers: REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Build the independently testable backend foundation for versioned project rules, approved project data, conflicts, project roles, and auditable rule evaluations without changing the existing document editor or chat UI.

**Architecture:** Add focused rules and project_data domain modules to the existing FastAPI/SQLModel monolith. Store immutable rule-set versions and project bindings separately from the existing AuditCase; store one current project field value plus append-only candidate, revision, and conflict records; use the existing tenant, user, AuditCase, and AuditCaseEvent boundaries for authorization and audit. Existing projects remain readable and usable through compatibility fallbacks until an administrator explicitly initializes their new rule/data state.

**Tech Stack:** Python 3.11+, FastAPI, Pydantic v2, SQLModel, SQLite-compatible incremental schema creation and backfill, pytest, Ruff.

**Spec:** docs/superpowers/specs/2026-09-04-rule-library-project-data-onlyoffice-design.md

## Global Constraints

- Keep the existing AuditCase, /api/audit-cases, chat binding, material processing, evidence, report, and knowledge retrieval contracts backward compatible.
- Use the existing StaffDeck tenant and User records; do not introduce a second identity system.
- Treat AuditCase owner and tenant administrators as project administrators unless an explicit audit_case_member_roles row says otherwise.
- Treat AI/model-assisted results as candidates or human-review states; only deterministic, evidence-bounded rules may be stored as directly blocking rules.
- A published RuleSetVersion and a project rule binding are immutable snapshots; changing a rule requires a new version or an explicit migration.
- Do not use last-write-wins for project fields; compare expected revisions and create conflicts.
- Never write empty, failed, or OCR-unconfirmed values over an approved project value.
- Do not put rule definitions into the knowledge retrieval result or use vector similarity to decide rule applicability.
- Do not change the frontend, ONLYOFFICE integration, document storage, OCR pipeline, or PostgreSQL support in this phase.
- Use SQLModel.metadata.create_all(engine) for new tables and an idempotent SQLite backfill helper for legacy project-role rows; do not introduce Alembic or a new migration framework.
- Preserve all unrelated dirty worktree changes; stage only files belonging to the current task in each commit.
- Every behavior-changing edit starts with a failing test and ends with the focused test plus relevant regression tests passing.

## Current Repository Map

The implementer must use these existing seams rather than creating parallel ones:

- backend/app/db/models.py: existing Tenant, User, AuditCase, AuditCaseEvent, AuditReportVersion, material, evidence, and knowledge models.
- backend/app/db/database.py: init_db(), SQLModel.metadata.create_all(engine), SQLite runtime configuration, and idempotent migration helpers.
- backend/app/audit_cases/service.py: AuditCaseService, can_access, get_case_for_user, record_case_event, and material/project access behavior.
- backend/app/audit_cases/schema.py: Pydantic request/read models and audit-case error classes.
- backend/app/api/audit_cases.py: existing tenant-admin dependency, tenant_id query convention, _authorized_case, and API error mapping.
- backend/app/security/permissions.py: require_tenant_admin, ensure_tenant_admin, and existing account role semantics.
- backend/app/main.py: router registration and startup database initialization.
- backend/tests/test_audit_case_management.py: SQLite fixture and admin/member/other-tenant user setup.
- backend/tests/test_audit_case_migration.py: startup migration regression patterns.
- backend/tests/test_audit_case_api.py: API client/authentication patterns.

## Phase 1 Scope and Explicit Deferrals

Phase 1 delivers:

1. Rule-set, immutable rule-version, atomic rule, project-binding, evaluation, and exception persistence.
2. Stable project field definitions, current values, candidate values, revisions, and conflicts.
3. Project role rows with compatibility fallback for existing owner/member JSON data.
4. Tenant isolation, project-role authorization, optimistic revision checks, and audit events.
5. API contracts for rule drafts/publishing, project rule bindings, project field candidates, conflict resolution, and basic deterministic evaluation.

Phase 1 explicitly defers:

- Rule library and project workbench React pages; these belong to Phase 2.
- ONLYOFFICE, DOCX content controls, document versions, save callbacks, and synchronization; these belong to Phase 4.
- RapidDoc/OCR coverage changes and report generation orchestration; these belong to Phase 3 and Phase 5.
- Automatic knowledge-scope redesign; Phase 1 preserves existing agent_default/custom behavior.
- PostgreSQL migration; it remains a separate production-hardening phase.

## File Map

Create:

- backend/app/rules/__init__.py: package boundary for the rule domain.
- backend/app/rules/schema.py: Pydantic request/read models, literals, and error codes.
- backend/app/rules/validation.py: pure rule validation and content fingerprinting.
- backend/app/rules/service.py: rule draft/version lifecycle, publish, bind, migration preview, and evaluation persistence.
- backend/app/project_data/__init__.py: package boundary for the project-data domain.
- backend/app/project_data/schema.py: field, candidate, value, conflict, source, and evaluation models.
- backend/app/project_data/fields.py: the 36 stable built-in field definitions and validators.
- backend/app/project_data/permissions.py: project-role literals and authorization.
- backend/app/project_data/service.py: candidate workflow, approved values, revision history, and conflict resolution.
- backend/app/api/rules.py: tenant-admin rule library endpoints and project rule binding endpoints.
- backend/app/api/project_data.py: field, candidate, conflict, evaluation, and exception endpoints.
- backend/tests/test_project_data_models.py: model constraints and JSON serialization.
- backend/tests/test_project_data_migration.py: legacy role backfill and idempotency.
- backend/tests/test_project_permissions.py: owner/member/reviewer/editor/tenant isolation.
- backend/tests/test_project_data_service.py: candidate state machine, revisions, approvals, and conflicts.
- backend/tests/test_rule_validation.py: rule validation and blocking constraints.
- backend/tests/test_rule_service.py: rule-set lifecycle and immutable versions.
- backend/tests/test_rule_binding_service.py: binding, version locking, migration, and evaluation.
- backend/tests/test_rule_api.py: rule library and binding API tests.
- backend/tests/test_project_data_api.py: field candidate and conflict API tests.
- backend/tests/test_phase1_rule_project_data_integration.py: public API end-to-end flow.

Modify:

- backend/app/db/models.py: add Phase 1 tables and indexes.
- backend/app/db/database.py: add idempotent legacy project-role backfill after create_all.
- backend/app/audit_cases/service.py: extend safe event metadata and retain the existing event seam.
- backend/app/main.py: include the new rules and project-data routers.
- backend/tests/test_audit_case_migration.py: verify startup backfill does not alter existing project fields.
- backend/tests/test_audit_case_management.py: verify legacy owner/member access when role rows are absent.

Do not modify frontend files in Phase 1.

---

### Task 1: Add Phase 1 persistence models and legacy role migration

**Files:**

- Create: backend/app/project_data/__init__.py
- Create: backend/app/rules/__init__.py
- Create: backend/tests/test_project_data_models.py
- Create: backend/tests/test_project_data_migration.py
- Modify: backend/app/db/models.py after AuditCaseEvent
- Modify: backend/app/db/database.py in init_db() and after _migrate_audit_case_schema
- Modify: backend/tests/test_audit_case_migration.py

**Interfaces:**

- Produces SQLModel tables AuditCaseMemberRole, RuleSet, RuleSetVersion, RuleDefinition, ProjectRuleBinding, ProjectDataFieldDefinition, ProjectDataValue, ProjectDataValueRevision, ProjectDataCandidate, ProjectDataConflict, RuleEvaluation, and RuleException.
- Produces _migrate_project_data_schema(conn, inspector, tables) -> None.
- Produces run_project_data_backfill(engine: Engine) -> None as a testable startup-migration seam that invokes the idempotent backfill.
- Produces role values project_admin, reviewer, editor, and viewer.
- Does not alter existing AuditCase columns or delete/rename legacy JSON data.

- [ ] Step 1: Write failing model tests

Add tests proving the new models and constraints are not yet available:

~~~python
def test_project_field_has_one_current_row_per_case_and_key(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'phase1-models.db'}")
    SQLModel.metadata.create_all(engine)

    with Session(engine) as db:
        db.add(ProjectDataValue(
            id="value-1",
            tenant_id="tenant_demo",
            audit_case_id="case-1",
            field_key="organization.legal_name",
            value_json="甲公司",
            status="approved",
            revision=1,
        ))
        db.commit()
        db.add(ProjectDataValue(
            id="value-2",
            tenant_id="tenant_demo",
            audit_case_id="case-1",
            field_key="organization.legal_name",
            value_json="乙公司",
            status="approved",
            revision=1,
        ))
        with pytest.raises(IntegrityError):
            db.commit()


def test_published_rule_version_has_immutable_identity_fields() -> None:
    version = RuleSetVersion(
        id="ruleset-version-1",
        tenant_id="tenant_demo",
        rule_set_id="ruleset-1",
        version=1,
        status="published",
        content_sha256="a" * 64,
    )
    assert version.status == "published"
    assert version.content_sha256 == "a" * 64
~~~

Run: backend/.venv/Scripts/python.exe -m pytest backend/tests/test_project_data_models.py -q

Expected: FAIL because the new model classes and constraints do not exist.

- [ ] Step 2: Implement the SQLModel tables

Add the models in backend/app/db/models.py using the existing new_id, utc_now, Column(JSON), Field(index=True), and UniqueConstraint patterns.

AuditCaseMemberRole must have tenant_id, audit_case_id, user_id, role, created_by_user_id, timestamps, and a unique tenant/case/user constraint.

ProjectDataValue must have tenant_id, audit_case_id, field_key, value_json, status, revision, source_json, approval/update actors, timestamps, and a unique tenant/case/field constraint.

RuleSet must have tenant_id, stable key, name, description, management_systems_json, audit_types_json, business_domain, status, timestamps, and a unique tenant/key constraint.

RuleSetVersion must have tenant_id, rule_set_id, integer version, status, content_sha256, published_by_user_id, published_at, timestamps, and a unique rule-set/version constraint.

RuleDefinition must have tenant_id, rule_set_version_id, rule_key, name, description, workflow_nodes_json, information_domains_json, document_types_json, field_keys_json, execution_level, execution_method, condition_json, input_requirements_json, evidence_requirements_json, source_refs_json, sequence, enabled, timestamps, and a unique version/rule-key constraint.

ProjectRuleBinding must have tenant_id, audit_case_id, rule_set_id, rule_set_version_id, selection_source, status, priority, bound_by_user_id, bound_at, supersedes_binding_id, and timestamps. Do not rely on a partial SQLite unique index; the service will maintain one current binding per project/rule-set transactionally.

ProjectDataFieldDefinition must have optional tenant_id, field_key, label, value_type, information_domain, scope, required, editable, sync_policy, validator_name, status, and timestamps. Resolve tenant-specific definitions before system definitions.

ProjectDataValueRevision must have tenant_id, audit_case_id, field_key, revision, value_json, status, source_json, operation, actor_user_id, created_at, and a unique case/field/revision constraint.

ProjectDataCandidate must have tenant_id, audit_case_id, field_key, value_json, source_json, status, expected_revision, submit/decision actors, decision_reason, and timestamps.

ProjectDataConflict must have tenant_id, audit_case_id, field_key, status, current_revision, candidate_ids_json, resolved_candidate_id, resolved_by_user_id, resolution_reason, and timestamps.

RuleEvaluation must have tenant_id, audit_case_id, rule_set_version_id, rule_definition_id, workflow_node, information_domain, target_ref, input_revision, status, result_json, evidence_refs_json, executor_type, executor_version, and timestamps.

RuleException must have tenant_id, audit_case_id, rule_evaluation_id, reason, evidence_refs_json, granted_by_user_id, and created_at.

Persist status values as strings and validate them in Pydantic/service code to remain compatible with existing SQLModel patterns.

- [ ] Step 3: Run model tests to verify constraints

Run: backend/.venv/Scripts/python.exe -m pytest backend/tests/test_project_data_models.py -q

Expected: PASS for table creation, JSON persistence, and the one-current-value uniqueness constraint.

- [ ] Step 4: Write the failing migration test

Create a legacy database test with an AuditCase that has owner_user_id and member_user_ids_json but no role rows. Invoke the startup migration seam and run it twice:

~~~python
def test_project_data_migration_backfills_owner_and_legacy_members(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(AuditCase(
            id="case-1",
            tenant_id="tenant_demo",
            owner_user_id="owner-1",
            member_user_ids_json=["member-1"],
            organization_name="甲公司",
            report_type="认证审核",
        ))
        db.commit()

    run_project_data_backfill(engine)
    run_project_data_backfill(engine)

    with Session(engine) as db:
        rows = db.exec(
            select(AuditCaseMemberRole)
            .where(AuditCaseMemberRole.audit_case_id == "case-1")
        ).all()
        assert {(row.user_id, row.role) for row in rows} == {
            ("owner-1", "project_admin"),
            ("member-1", "editor"),
        }
~~~

Run: backend/.venv/Scripts/python.exe -m pytest backend/tests/test_project_data_migration.py -q

Expected: FAIL because the backfill function does not exist.

- [ ] Step 5: Implement the idempotent backfill

Add _migrate_project_data_schema(conn, inspector, tables) -> None in backend/app/db/database.py and call it after create_all has created the new tables and after _migrate_audit_case_schema has completed.

For every current AuditCase:

1. Insert owner_user_id as project_admin when absent.
2. Insert each distinct member_user_ids_json user as editor when absent.
3. Never downgrade an existing explicit role.
4. Require the user row to match the case tenant before inserting; skip malformed legacy IDs and retain a diagnostic log entry.
5. Record migration marker project_data_member_roles_v1 when the existing app_data_migrations table is available.

The helper must be safe when the marker is already present and when audit_cases is empty. It must not add columns to audit_cases.

- [ ] Step 6: Run migration and audit-case regressions

Run:

~~~powershell
backend/.venv/Scripts/python.exe -m pytest backend/tests/test_project_data_migration.py backend/tests/test_audit_case_migration.py backend/tests/test_audit_case_management.py -q
~~~

Expected: PASS; existing owner/member access remains unchanged.

- [ ] Step 7: Commit the persistence foundation

~~~powershell
git add backend/app/db/models.py backend/app/db/database.py backend/app/project_data/__init__.py backend/app/rules/__init__.py backend/tests/test_project_data_models.py backend/tests/test_project_data_migration.py backend/tests/test_audit_case_migration.py backend/tests/test_audit_case_management.py
git commit -m "feat: add project data and rule persistence foundation"
~~~

### Task 2: Add project roles and authorization seams

**Files:**

- Create: backend/app/project_data/permissions.py
- Create: backend/tests/test_project_permissions.py
- Modify: backend/app/audit_cases/service.py only where the reusable access seam is needed
- Modify: backend/tests/test_audit_case_management.py for compatibility assertions

**Interfaces:**

- ProjectRole is Literal["project_admin", "reviewer", "editor", "viewer"].
- resolve_project_role(db: Session, case: AuditCase, user: User) -> str | None.
- ensure_project_role(db: Session, case: AuditCase, user: User, allowed: set[str]) -> User.
- can_submit_project_candidate(db: Session, case: AuditCase, user: User) -> bool.
- can_approve_project_candidate(db: Session, case: AuditCase, user: User) -> bool.
- Tenant administrators remain globally authorized after tenant equality is confirmed.

- [ ] Step 1: Write failing authorization tests

~~~python
def test_owner_is_project_admin_without_role_row(permission_context) -> None:
    db, owner, _member, _reviewer, case = permission_context
    assert resolve_project_role(db, case, owner) == "project_admin"


def test_explicit_reviewer_can_approve_but_editor_cannot(permission_context) -> None:
    db, _owner, editor, reviewer, case = permission_context
    assert can_submit_project_candidate(db, case, editor) is True
    assert can_approve_project_candidate(db, case, editor) is False
    assert can_approve_project_candidate(db, case, reviewer) is True


def test_other_tenant_user_is_not_a_project_member(permission_context) -> None:
    db, _owner, _editor, _reviewer, case = permission_context
    other = _user("other", tenant_id="tenant_other", role="member")
    db.add(other)
    db.commit()
    assert resolve_project_role(db, case, other) is None
~~~

Run: backend/.venv/Scripts/python.exe -m pytest backend/tests/test_project_permissions.py -q

Expected: FAIL because role resolution and explicit reviewer support do not exist.

- [ ] Step 2: Implement role resolution with legacy fallback

Implement resolve_project_role in this exact order:

1. Return None when user.tenant_id differs from case.tenant_id.
2. Return project_admin for user.role == admin after tenant equality is confirmed.
3. Return the explicit AuditCaseMemberRole.role when a valid row exists.
4. Return project_admin when case.owner_user_id == user.id.
5. Return editor when user.id is in case.member_user_ids_json.
6. Return None otherwise.

ensure_project_role must raise AuditCaseAccessDenied("PROJECT_ROLE_REQUIRED") for a missing or disallowed role and must not reveal whether a case in another tenant exists.

- [ ] Step 3: Run focused permission tests

Run: backend/.venv/Scripts/python.exe -m pytest backend/tests/test_project_permissions.py backend/tests/test_audit_case_management.py -q

Expected: PASS, including old projects without role rows.

- [ ] Step 4: Commit the authorization seam

~~~powershell
git add backend/app/project_data/permissions.py backend/app/audit_cases/service.py backend/tests/test_project_permissions.py backend/tests/test_audit_case_management.py
git commit -m "feat: add project role authorization"
~~~

### Task 3: Add stable field definitions and the candidate/conflict workflow

**Files:**

- Create: backend/app/project_data/schema.py
- Create: backend/app/project_data/fields.py
- Create: backend/app/project_data/service.py
- Create: backend/tests/test_project_data_service.py
- Modify: backend/tests/test_project_data_models.py for schema serialization

**Interfaces:**

- SourceRef contains material_id, material_version_id, document_id, location, and evidence_excerpt.
- ProjectDataCandidateCreate(field_key: str, value: Any, source: SourceRef, expected_revision: int | None, note: str | None).
- ProjectDataService.submit_candidate(case: AuditCase, actor: User, request: ProjectDataCandidateCreate) -> ProjectDataCandidate.
- ProjectDataService.approve_candidate(candidate_id: str, actor: User, expected_current_revision: int | None, reason: str | None) -> ProjectDataValue.
- ProjectDataService.reject_candidate(candidate_id: str, actor: User, reason: str) -> ProjectDataCandidate.
- ProjectDataService.list_fields(case: AuditCase, actor: User) -> list[ProjectDataValueRead].
- ProjectDataService.list_conflicts(case: AuditCase, actor: User) -> list[ProjectDataConflictRead].
- ProjectDataService.resolve_conflict(conflict_id: str, selected_candidate_id: str, actor: User, reason: str) -> ProjectDataValue.
- validate_field_value(definition: FieldDefinition, value: Any) -> None raises ProjectFieldValidationError with a stable code.
- validate_candidate_request(request: ProjectDataCandidateCreate) -> None rejects empty values and non-auditable sources.

- [ ] Step 1: Define the field schema and write failing validation tests

Create FieldDefinition as a frozen Pydantic model with field_key, label, value_type, information_domain, scope, required, editable, sync_policy, and validator_name.

Register these 36 system field keys in SYSTEM_FIELD_DEFINITIONS:

~~~text
organization.legal_name
organization.registered_address
organization.credit_code
organization.contact_person
organization.contact_phone
certification_project.audit_type
certification_project.management_system
certification_project.scope
certification_project.applicable_standard
certification_project.audit_objective
certification_project.audit_stage
audit_event.start_date
audit_event.end_date
audit_event.team_leader
audit_event.audit_team_members
audit_event.audit_team_roles
audit_event.site_ids
audit_event.audit_days
audit_event.opening_time
audit_event.closing_time
site.permanent_sites
site.temporary_sites
energy.review.boundary
energy.review.significant_energy_uses
energy.review.baseline_period
energy.review.performance_indicators
energy.review.objectives
energy.review.targets
energy.review.action_plans
audit_evidence.nonconformities
audit_evidence.improvement_suggestions
audit_evidence.observations
audit_evidence.conclusion
document.number
document.revision
document.issue_date
document.approver
~~~

Write these tests:

~~~python
def test_field_keys_are_stable_and_unique() -> None:
    keys = [item.field_key for item in SYSTEM_FIELD_DEFINITIONS]
    assert len(keys) == 36
    assert len(keys) == len(set(keys))
    assert "certification_project.scope" in keys


def test_date_field_rejects_non_iso_date() -> None:
    definition = get_field_definition("audit_event.start_date", tenant_id="tenant_demo")
    with pytest.raises(ProjectFieldValidationError, match="INVALID_DATE"):
        validate_field_value(definition, "2026/09/04")


def test_failed_or_empty_source_cannot_be_approved() -> None:
    request = ProjectDataCandidateCreate(
        field_key="organization.legal_name",
        value="",
        source=SourceRef(material_id="material-1", location="page:1", evidence_excerpt=""),
    )
    with pytest.raises(ProjectFieldValidationError, match="EMPTY_VALUE"):
        validate_candidate_request(request)
~~~

Run: backend/.venv/Scripts/python.exe -m pytest backend/tests/test_project_data_service.py -q

Expected: FAIL because field definitions, schemas, and validators do not exist.

- [ ] Step 2: Implement field definitions and validators

Use these value types in Phase 1: text, date, datetime, integer, number, boolean, list, and object. Implement validators for ISO date/datetime, positive numeric values, non-empty text, and list/object shape.

A source must contain a non-empty location and evidence excerpt for a candidate to be approvable. The API may accept a candidate with a missing source only when the field definition explicitly sets source_optional=True; none of the 36 core fields sets that flag.

Resolve tenant-specific definitions first, then system definitions. Reject unknown keys with UNKNOWN_FIELD_KEY. Keep labels separate from field keys.

- [ ] Step 3: Write failing candidate lifecycle tests

~~~python
def test_candidate_is_pending_and_approval_creates_revision(service_context) -> None:
    db, case, editor, reviewer = service_context
    candidate = ProjectDataService(db).submit_candidate(
        case,
        editor,
        ProjectDataCandidateCreate(
            field_key="organization.legal_name",
            value="甲公司",
            source=SourceRef(material_id="material-1", location="page:2", evidence_excerpt="甲公司"),
            expected_revision=0,
        ),
    )
    assert candidate.status == "pending"

    approved = ProjectDataService(db).approve_candidate(
        candidate.id, reviewer, expected_current_revision=0, reason="资料确认",
    )
    assert approved.value_json == "甲公司"
    assert approved.status == "approved"
    assert approved.revision == 1


def test_stale_candidate_creates_conflict_instead_of_overwriting(service_context) -> None:
    db, case, editor, reviewer = service_context
    first = _submit_and_approve(db, case, editor, reviewer, "甲公司")
    second = ProjectDataService(db).submit_candidate(
        case,
        editor,
        ProjectDataCandidateCreate(
            field_key="organization.legal_name",
            value="乙公司",
            source=SourceRef(material_id="material-2", location="page:1", evidence_excerpt="乙公司"),
            expected_revision=first.revision,
        ),
    )
    _submit_and_approve(db, case, editor, reviewer, "丙公司")

    with pytest.raises(ProjectDataConflictError, match="PROJECT_DATA_CONFLICT"):
        ProjectDataService(db).approve_candidate(
            second.id, reviewer, expected_current_revision=first.revision, reason="冲突测试",
        )

    conflict = ProjectDataService(db).list_conflicts(case, reviewer)[0]
    assert conflict.status == "open"
    current = db.exec(
        select(ProjectDataValue).where(
            ProjectDataValue.audit_case_id == case.id,
            ProjectDataValue.field_key == "organization.legal_name",
        )
    ).one()
    assert current.value_json == "甲公司"


def test_rejected_candidate_never_changes_approved_value(service_context) -> None:
    db, case, editor, reviewer = service_context
    current = _submit_and_approve(db, case, editor, reviewer, "甲公司")
    candidate = _submit(db, case, editor, "错误值", expected_revision=current.revision)
    ProjectDataService(db).reject_candidate(candidate.id, reviewer, "证据不足")
    db.refresh(current)
    assert current.value_json == "甲公司"
    assert current.revision == 1
~~~

Run: backend/.venv/Scripts/python.exe -m pytest backend/tests/test_project_data_service.py -q

Expected: FAIL because the candidate state machine and revision checks do not exist.

- [ ] Step 4: Implement candidate, approval, history, and conflict services

Implement these rules transactionally:

1. Candidate submission validates tenant/project access, field key, value type, source, and expected revision; it creates pending and never changes the current value.
2. Approval requires project admin, reviewer, or tenant admin. It reloads the current value inside the transaction and compares expected_current_revision to the current revision.
3. When the expected revision matches, create or update ProjectDataValue, increment revision, append ProjectDataValueRevision, mark the candidate approved, and audit the operation.
4. When the expected revision is stale or another pending candidate has a different value for the same field, create ProjectDataConflict and raise PROJECT_DATA_CONFLICT; do not change the approved value.
5. Rejection marks the candidate rejected, requires a non-empty reason, and does not change the current value.
6. Conflict resolution validates that the selected candidate belongs to the conflict, applies it as the next revision, marks the conflict resolved, and records the reason.
7. Writes use one SQLModel session transaction and are safe to retry only when the candidate ID and target revision have not already been finalized.

Use ProjectDataValue.status values proposed, validated, approved, superseded, and rejected. Model-assisted extraction still enters the candidate path.

- [ ] Step 5: Run service and regression tests

Run:

~~~powershell
backend/.venv/Scripts/python.exe -m pytest backend/tests/test_project_data_service.py backend/tests/test_project_permissions.py backend/tests/test_audit_cases.py -q
~~~

Expected: PASS; no old audit-case access or material behavior changes.

- [ ] Step 6: Commit the project data domain

~~~powershell
git add backend/app/project_data/schema.py backend/app/project_data/fields.py backend/app/project_data/service.py backend/tests/test_project_data_service.py backend/tests/test_project_data_models.py
git commit -m "feat: add project data candidate workflow"
~~~

### Task 4: Add rule-set validation and immutable publishing

**Files:**

- Create: backend/app/rules/schema.py
- Create: backend/app/rules/validation.py
- Create: backend/app/rules/service.py
- Create: backend/tests/test_rule_validation.py
- Create: backend/tests/test_rule_service.py

**Interfaces:**

- RuleExecutionLevel is Literal["mandatory", "warning", "guidance"].
- RuleExecutionMethod is Literal["deterministic", "model_assisted"].
- RuleSetCreate contains tenant_id, key, name, description, management_systems, audit_types, and business_domain.
- RuleDefinitionCreate contains rule_key, name, description, workflow_nodes, information_domains, document_types, field_keys, execution_level, execution_method, condition, input_requirements, evidence_requirements, source_refs, sequence, and enabled.
- validate_rule_definition(rule: RuleDefinitionCreate) -> None.
- validate_rule_set(rules: list[RuleDefinitionCreate]) -> None.
- fingerprint_rule_set_version(rules: list[RuleDefinitionCreate]) -> str.
- RuleLibraryService.create_rule_set(actor: User, request: RuleSetCreate) -> RuleSet.
- RuleLibraryService.create_draft_version(rule_set: RuleSet, actor: User, rules: list[RuleDefinitionCreate]) -> RuleSetVersion.
- RuleLibraryService.validate_version(version_id: str, actor: User) -> list[str].
- RuleLibraryService.publish_version(version_id: str, actor: User) -> RuleSetVersion.
- RuleLibraryService.replace_rules(version_id: str, actor: User, rules: list[RuleDefinitionCreate]) -> RuleSetVersion.

- [ ] Step 1: Write failing rule validation tests

~~~python
def test_mandatory_model_assisted_rule_is_rejected() -> None:
    rule = _rule(execution_level="mandatory", execution_method="model_assisted")
    with pytest.raises(RuleValidationError, match="MANDATORY_RULE_MUST_BE_DETERMINISTIC"):
        validate_rule_definition(rule)


def test_mandatory_rule_requires_source_and_evidence() -> None:
    rule = _rule(
        execution_level="mandatory",
        execution_method="deterministic",
        source_refs=[],
        evidence_requirements=[],
    )
    with pytest.raises(RuleValidationError, match="MANDATORY_RULE_SOURCE_REQUIRED"):
        validate_rule_definition(rule)


def test_rule_keys_are_unique_within_a_version() -> None:
    with pytest.raises(RuleValidationError, match="DUPLICATE_RULE_KEY"):
        validate_rule_set([_rule(rule_key="scope.required"), _rule(rule_key="scope.required")])


def test_fingerprint_is_order_independent_after_sequence_normalization() -> None:
    first = fingerprint_rule_set_version([_rule(rule_key="a"), _rule(rule_key="b")])
    second = fingerprint_rule_set_version([_rule(rule_key="b"), _rule(rule_key="a")])
    assert first == second
~~~

Run: backend/.venv/Scripts/python.exe -m pytest backend/tests/test_rule_validation.py -q

Expected: FAIL because the rule schemas and validation functions do not exist.

- [ ] Step 2: Implement schemas and validation

Validation must enforce:

- non-empty stable rule_key, maximum 160 characters, and only lowercase ASCII letters, digits, underscore, dot, and hyphen;
- at least one workflow node and information domain;
- mandatory rules use deterministic execution;
- mandatory rules contain at least one source reference and one evidence requirement;
- model_assisted rules can only return pass, fail, or indeterminate for human review and cannot be published as a direct blocking result;
- field references use known stable field keys or a declared tenant field definition;
- conditions use only required, equals, not_equals, in, gte, lte, matches, and has_evidence;
- duplicate rule keys within a version are rejected;
- source references include a knowledge base version/document/section or an explicitly recorded internal-policy reference;
- sequence is non-negative and enabled is boolean.

Normalize rules by rule_key, then sequence, then canonical JSON with sorted keys before hashing SHA-256. The fingerprint must not include secrets, timestamps, or the drafting user.

- [ ] Step 3: Write failing rule lifecycle tests

~~~python
def test_only_tenant_admin_can_publish(rule_context) -> None:
    db, admin, member, rule_set = rule_context
    draft = RuleLibraryService(db).create_draft_version(rule_set, admin, [_rule_create()])
    with pytest.raises(RuleAccessDenied, match="RULE_ADMIN_REQUIRED"):
        RuleLibraryService(db).publish_version(draft.id, member)


def test_published_version_cannot_be_edited_in_place(rule_context) -> None:
    db, admin, _member, rule_set = rule_context
    draft = RuleLibraryService(db).create_draft_version(rule_set, admin, [_rule_create()])
    published = RuleLibraryService(db).publish_version(draft.id, admin)
    with pytest.raises(RuleVersionImmutableError, match="RULE_VERSION_IMMUTABLE"):
        RuleLibraryService(db).replace_rules(published.id, admin, [_rule_create(name="新名称")])


def test_publish_stores_content_hash_and_published_actor(rule_context) -> None:
    db, admin, _member, rule_set = rule_context
    draft = RuleLibraryService(db).create_draft_version(rule_set, admin, [_rule_create()])
    published = RuleLibraryService(db).publish_version(draft.id, admin)
    assert len(published.content_sha256) == 64
    assert published.published_by_user_id == admin.id
    assert published.status == "published"
~~~

Run: backend/.venv/Scripts/python.exe -m pytest backend/tests/test_rule_service.py -q

Expected: FAIL because the rule-set lifecycle and immutability are not implemented.

- [ ] Step 4: Implement rule-set draft and publish services

Implement:

1. create_rule_set validates tenant ownership, stable key uniqueness, and starts status active.
2. create_draft_version creates the next integer version, validates all rules, replaces only a draft version, computes the canonical fingerprint, and persists rule definitions tied to the version.
3. validate_version returns stable error codes without changing persistence.
4. publish_version requires tenant admin, reruns validation in the same transaction, sets published status, stores publisher/time/hash, and rejects a second publish.
5. replace_rules is available only for drafts and raises RULE_VERSION_IMMUTABLE for published versions.
6. A failed publish leaves the draft and previous published version unchanged.

- [ ] Step 5: Run rule validation and lifecycle tests

Run: backend/.venv/Scripts/python.exe -m pytest backend/tests/test_rule_validation.py backend/tests/test_rule_service.py -q

Expected: PASS.

- [ ] Step 6: Commit the rule lifecycle

~~~powershell
git add backend/app/rules/schema.py backend/app/rules/validation.py backend/app/rules/service.py backend/tests/test_rule_validation.py backend/tests/test_rule_service.py
git commit -m "feat: add immutable rule set publishing"
~~~

### Task 5: Add project rule binding, version locking, and deterministic evaluation

**Files:**

- Modify: backend/app/rules/service.py
- Create: backend/tests/test_rule_binding_service.py
- Modify: backend/app/project_data/schema.py for evaluation context/read models

**Interfaces:**

- RuleBindingService.bind_published_version(case: AuditCase, version_id: str, actor: User, selection_source: Literal["recommended", "manual"]) -> ProjectRuleBinding.
- RuleBindingService.replace_current_bindings(case: AuditCase, version_ids: list[str], actor: User, selection_source: Literal["recommended", "manual"]) -> list[ProjectRuleBinding].
- RuleBindingService.list_current_bindings(case: AuditCase, actor: User) -> list[ProjectRuleBinding].
- RuleBindingService.preview_migration(case: AuditCase, target_version_ids: list[str], actor: User) -> RuleMigrationPreview.
- RuleBindingService.migrate(case: AuditCase, target_version_ids: list[str], actor: User, reason: str) -> list[ProjectRuleBinding].
- RuleEvaluationContext contains project_fields, evidence_refs, workflow_node, information_domain, and target_ref.
- evaluate_deterministic_rule(rule: RuleDefinition, context: RuleEvaluationContext) -> DeterministicRuleResult.
- evaluate_rule_without_model(rule: RuleDefinition, context: RuleEvaluationContext) -> DeterministicRuleResult.
- RuleLibraryService.evaluate_project(case: AuditCase, actor: User, context: RuleEvaluationContext) -> list[RuleEvaluation].

- [ ] Step 1: Write failing binding tests

~~~python
def test_project_can_bind_only_published_rule_versions(binding_context) -> None:
    db, admin, case, draft, published = binding_context
    with pytest.raises(RuleBindingError, match="PUBLISHED_RULE_VERSION_REQUIRED"):
        RuleBindingService(db).bind_published_version(case, draft.id, admin, "manual")

    binding = RuleBindingService(db).bind_published_version(case, published.id, admin, "manual")
    assert binding.rule_set_version_id == published.id
    assert binding.status == "current"


def test_existing_binding_is_not_silently_replaced(binding_context) -> None:
    db, admin, case, _draft, first = binding_context
    second = _publish_second_version(db, admin)
    RuleBindingService(db).bind_published_version(case, first.id, admin, "manual")

    with pytest.raises(RuleBindingMigrationRequired, match="RULE_MIGRATION_CONFIRMATION_REQUIRED"):
        RuleBindingService(db).bind_published_version(case, second.id, admin, "manual")


def test_migration_creates_new_binding_and_preserves_old_snapshot(binding_context) -> None:
    db, admin, case, _draft, first = binding_context
    second = _publish_second_version(db, admin)
    old = RuleBindingService(db).bind_published_version(case, first.id, admin, "manual")
    preview = RuleBindingService(db).preview_migration(case, [second.id], admin)
    assert preview.changed_rule_keys

    current = RuleBindingService(db).migrate(case, [second.id], admin, "规则修订")
    assert current[0].rule_set_version_id == second.id
    db.refresh(old)
    assert old.status == "superseded"
~~~

Run: backend/.venv/Scripts/python.exe -m pytest backend/tests/test_rule_binding_service.py -q

Expected: FAIL because binding and migration behavior do not exist.

- [ ] Step 2: Implement version-pinned binding

Implement:

1. The target version must belong to the current tenant, be published, and have a valid immutable hash.
2. A binding stores the exact rule-set version ID, actor, source, priority, and timestamp.
3. If a current binding for the same rule set exists, direct replacement raises RULE_MIGRATION_CONFIRMATION_REQUIRED and changes nothing.
4. preview_migration compares old/new rule keys and canonical fingerprints, returning added, removed, changed, unchanged, and impacted information domains/workflow nodes.
5. migrate requires a non-empty reason, marks the old binding superseded, creates a new current binding, and writes an audit event.
6. If two current mandatory rules with the same scope have contradictory condition/output metadata, binding validation raises CONFLICTING_MANDATORY_RULES; priority cannot resolve the contradiction.
7. A project with no binding remains compatible and is reported as RULE_BINDING_NOT_INITIALIZED; this phase does not auto-bind existing projects.

- [ ] Step 3: Write failing deterministic evaluation tests

~~~python
def test_required_rule_fails_when_field_is_missing() -> None:
    rule = _rule_definition(
        execution_level="mandatory",
        execution_method="deterministic",
        condition={"operator": "required", "field_key": "certification_project.scope"},
    )
    result = evaluate_deterministic_rule(
        rule,
        RuleEvaluationContext(
            project_fields={},
            evidence_refs=[],
            workflow_node="collect",
            information_domain="scope",
            target_ref=None,
        ),
    )
    assert result.status == "failed"
    assert result.blocking is True


def test_warning_rule_does_not_block() -> None:
    rule = _rule_definition(
        execution_level="warning",
        execution_method="deterministic",
        condition={"operator": "required", "field_key": "certification_project.scope"},
    )
    result = evaluate_deterministic_rule(
        rule,
        RuleEvaluationContext(
            project_fields={},
            evidence_refs=[],
            workflow_node="collect",
            information_domain="scope",
            target_ref=None,
        ),
    )
    assert result.status == "warning"
    assert result.blocking is False


def test_model_assisted_result_is_human_review_only() -> None:
    rule = _rule_definition(execution_level="guidance", execution_method="model_assisted")
    result = evaluate_rule_without_model(rule, _context())
    assert result.status == "indeterminate"
    assert result.blocking is False
~~~

Run: backend/.venv/Scripts/python.exe -m pytest backend/tests/test_rule_binding_service.py -q

Expected: FAIL because the deterministic evaluator does not exist.

- [ ] Step 4: Implement the narrow deterministic evaluator

Support only required, equals, not_equals, in, gte, lte, matches, and has_evidence in Phase 1. Return status (passed, failed, warning, indeterminate), blocking, message, and evidence_refs.

Rules with execution_method=model_assisted return indeterminate without calling a model. Rules with execution_level=mandatory set blocking true only when execution_method=deterministic and the condition result is a definite failure. Persist every evaluation with the exact project field revision and evidence refs.

Do not block the existing report API in Phase 1; expose evaluation results and leave report-finalization integration to Phase 5.

- [ ] Step 5: Run binding and evaluator tests

Run: backend/.venv/Scripts/python.exe -m pytest backend/tests/test_rule_binding_service.py backend/tests/test_rule_service.py -q

Expected: PASS.

- [ ] Step 6: Commit project rule binding and evaluation

~~~powershell
git add backend/app/rules/service.py backend/app/project_data/schema.py backend/tests/test_rule_binding_service.py
git commit -m "feat: pin project rule versions and evaluate rules"
~~~

### Task 6: Expose Phase 1 API contracts with tenant and project isolation

**Files:**

- Create: backend/app/api/rules.py
- Create: backend/app/api/project_data.py
- Create: backend/tests/test_rule_api.py
- Create: backend/tests/test_project_data_api.py
- Modify: backend/app/main.py
- Modify: backend/app/audit_cases/service.py for the event metadata allow-list

**Interfaces:**

Rule library endpoints, all requiring tenant admin:

~~~text
POST /api/rule-sets
POST /api/rule-sets/{rule_set_id}/versions
POST /api/rule-sets/{rule_set_id}/versions/{version_id}/validate
POST /api/rule-sets/{rule_set_id}/versions/{version_id}/publish
GET  /api/rule-sets/{rule_set_id}/versions
~~~

Project rule endpoints:

~~~text
GET  /api/audit-cases/{case_id}/rule-bindings
PUT  /api/audit-cases/{case_id}/rule-bindings
POST /api/audit-cases/{case_id}/rule-bindings/migration-preview
POST /api/audit-cases/{case_id}/rule-bindings/migrate
GET  /api/audit-cases/{case_id}/rule-evaluations
POST /api/rule-evaluations/{evaluation_id}/exception
~~~

Project data endpoints:

~~~text
GET  /api/audit-cases/{case_id}/field-schema
GET  /api/audit-cases/{case_id}/fields
GET  /api/audit-cases/{case_id}/field-candidates
POST /api/audit-cases/{case_id}/field-candidates
POST /api/field-candidates/{candidate_id}/approve
POST /api/field-candidates/{candidate_id}/reject
GET  /api/audit-cases/{case_id}/field-conflicts
POST /api/field-conflicts/{conflict_id}/resolve
~~~

Every project endpoint uses the existing tenant_id query parameter and authorized-case validation. Request/response names must match the services from Tasks 2–5.

- [ ] Step 1: Write failing API authorization and lifecycle tests

~~~python
def test_non_admin_cannot_create_rule_set(api_context) -> None:
    client, _admin_headers, member_headers = api_context
    response = client.post(
        "/api/rule-sets",
        json={
            "tenant_id": "tenant_demo",
            "key": "enms.audit",
            "name": "能源管理体系审核规则",
            "management_systems": ["能源管理体系"],
            "audit_types": ["认证审核"],
            "business_domain": "audit",
        },
        headers=member_headers,
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "RULE_ADMIN_REQUIRED"


def test_candidate_submission_returns_pending_candidate(api_context) -> None:
    client, member_headers, case = api_context
    response = client.post(
        f"/api/audit-cases/{case.id}/field-candidates?tenant_id=tenant_demo",
        json={
            "field_key": "organization.legal_name",
            "value": "甲公司",
            "source": {
                "material_id": "material-1",
                "location": "page:2",
                "evidence_excerpt": "甲公司",
            },
            "expected_revision": 0,
        },
        headers=member_headers,
    )
    assert response.status_code == 201
    assert response.json()["status"] == "pending"


def test_stale_approval_returns_conflict_without_overwriting_value(api_context) -> None:
    client, admin_headers, case = api_context
    first = _create_and_approve_candidate(client, admin_headers, case.id, "甲公司", 0)
    stale = _create_candidate(client, admin_headers, case.id, "乙公司", first["revision"])
    _create_and_approve_candidate(client, admin_headers, case.id, "丙公司", first["revision"])

    response = client.post(
        f"/api/field-candidates/{stale['id']}/approve?tenant_id=tenant_demo",
        json={
            "expected_current_revision": first["revision"],
            "reason": "冲突测试",
        },
        headers=admin_headers,
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "PROJECT_DATA_CONFLICT"
~~~

Run:

~~~powershell
backend/.venv/Scripts/python.exe -m pytest backend/tests/test_rule_api.py backend/tests/test_project_data_api.py -q
~~~

Expected: FAIL because the routers are not registered.

- [ ] Step 2: Implement schemas and router dependency boundaries

Use require_tenant_admin for rule-set create/version/publish endpoints. Use get_current_user plus ensure_current_user_tenant and ensure_project_role for project binding/migration, field reads/submission/approval, conflict operations, and rule exceptions. Only project_admin, reviewer, or tenant admin can bind or migrate project rules.

A project member may submit candidates and view authorized project fields. Only project admin, reviewer, or tenant admin may approve or resolve.

Map domain errors to stable responses:

| Domain condition | HTTP | Detail |
| --- | ---: | --- |
| tenant or project access failure | 403/404 | AUDIT_CASE_NOT_FOUND or PROJECT_ROLE_REQUIRED |
| rule-set admin missing | 403 | RULE_ADMIN_REQUIRED |
| unknown field | 422 | UNKNOWN_FIELD_KEY |
| invalid value/source | 422 | INVALID_FIELD_VALUE, EMPTY_VALUE, or SOURCE_REQUIRED |
| stale revision | 409 | PROJECT_DATA_CONFLICT |
| published version edited | 409 | RULE_VERSION_IMMUTABLE |
| non-published binding target | 422 | PUBLISHED_RULE_VERSION_REQUIRED |
| binding replacement without migration | 409 | RULE_MIGRATION_CONFIRMATION_REQUIRED |
| conflicting mandatory rules | 422 | CONFLICTING_MANDATORY_RULES |
| invalid exception | 422 | RULE_EXCEPTION_NOT_ALLOWED |

Do not return rule source secrets, arbitrary exception traces, or cross-tenant existence information.

- [ ] Step 3: Implement rule-set and project-binding routers

In backend/app/api/rules.py:

1. Parse RuleSetCreate, RuleDefinitionCreate lists, and migration requests.
2. Resolve tenant equality before any tenant-scoped database lookup.
3. Reuse AuditCaseService.get_case_for_user or the existing authorized-case behavior.
4. Return published versions and bindings as read models with IDs and statuses.
5. Return migration preview diffs without mutation; require a non-empty migration reason.
6. For rule exceptions, allow only evaluations whose rule explicitly permits exceptions and only authorized reviewers/project administrators/tenant administrators.

- [ ] Step 4: Implement project-data routers

In backend/app/api/project_data.py:

1. Return system plus tenant field definitions from field-schema.
2. Return current values and status from fields; never synthesize a value from a missing row.
3. Return candidates and conflicts with source location and revision information.
4. Submit candidates with status 201; do not update current values in the submission handler.
5. Approve, reject, and resolve through ProjectDataService.
6. Extend the existing safe event metadata allow-list with field_key, candidate_id, conflict_id, rule_set_version_id, rule_evaluation_id, revision, and status. Continue rejecting arbitrary metadata.

Register both routers in backend/app/main.py after the existing API routers. Keep /api/audit-cases unchanged.

- [ ] Step 5: Run API tests and existing API regressions

Run:

~~~powershell
backend/.venv/Scripts/python.exe -m pytest backend/tests/test_rule_api.py backend/tests/test_project_data_api.py backend/tests/test_audit_case_api.py backend/tests/test_audit_case_management.py -q
~~~

Expected: PASS. Verify that existing list/create/material/report endpoints retain their prior status codes and response shape.

- [ ] Step 6: Commit the Phase 1 APIs

~~~powershell
git add backend/app/api/rules.py backend/app/api/project_data.py backend/app/main.py backend/app/audit_cases/service.py backend/tests/test_rule_api.py backend/tests/test_project_data_api.py backend/tests/test_audit_case_api.py
git commit -m "feat: expose rule and project data APIs"
~~~

### Task 7: Add Phase 1 integration verification and implementation notes

**Files:**

- Create: backend/tests/test_phase1_rule_project_data_integration.py
- Modify: docs/superpowers/plans/2026-09-04-phase1-rule-library-project-data-foundation.md by checking completed steps during execution
- Modify: docs/superpowers/specs/2026-09-04-rule-library-project-data-onlyoffice-design.md only when implementation reveals a verified contract correction

**Interfaces:**

- The end-to-end test uses the public API and existing SQLite test setup.
- It does not import private implementation helpers except the existing fixture setup.
- No frontend or external service dependency is introduced.

- [ ] Step 1: Write the failing end-to-end test

Create one complete scenario:

~~~python
def test_phase1_project_rule_and_data_flow_is_auditable(phase1_client) -> None:
    client, admin_headers, member_headers = phase1_client
    rule_set = _create_rule_set(client, admin_headers)
    published = _create_publishable_version(client, admin_headers, rule_set["id"])
    case = _create_case(client, admin_headers, agent_id=None)

    binding = client.put(
        f"/api/audit-cases/{case['id']}/rule-bindings?tenant_id=tenant_demo",
        json={
            "rule_set_version_ids": [published["id"]],
            "selection_source": "manual",
        },
        headers=admin_headers,
    )
    assert binding.status_code == 200

    candidate = _submit_candidate(
        client, member_headers, case["id"], "甲公司", 0,
    )
    approved = _approve_candidate(
        client, admin_headers, candidate["id"], 0,
    )
    assert approved["status"] == "approved"

    events = client.get(
        f"/api/audit-cases/{case['id']}/events?tenant_id=tenant_demo",
        headers=admin_headers,
    )
    assert events.status_code == 200
    event_types = {item["event_type"] for item in events.json()}
    assert {
        "rule_binding_created",
        "project_field_candidate_created",
        "project_field_approved",
    } <= event_types
~~~

Run: backend/.venv/Scripts/python.exe -m pytest backend/tests/test_phase1_rule_project_data_integration.py -q

Expected: FAIL until all Phase 1 routers, event writes, and response contracts are connected.

- [ ] Step 2: Implement event and transaction assertions

Ensure these operations each commit one auditable event after their domain mutation succeeds:

- rule_set_created;
- rule_version_published;
- rule_binding_created or rule_binding_migrated;
- project_field_candidate_created;
- project_field_approved;
- project_field_rejected;
- project_field_conflict_detected;
- project_field_conflict_resolved;
- rule_evaluation_completed;
- rule_exception_granted.

Event metadata may contain only IDs, status, version, field key, counts, and stable error/status codes. Do not store full field values, evidence excerpts, rule text, prompts, or credentials in AuditCaseEvent.metadata_json.

- [ ] Step 3: Run the full Phase 1 test set

Run:

~~~powershell
backend/.venv/Scripts/python.exe -m pytest backend/tests/test_project_data_models.py backend/tests/test_project_data_migration.py backend/tests/test_project_permissions.py backend/tests/test_project_data_service.py backend/tests/test_rule_validation.py backend/tests/test_rule_service.py backend/tests/test_rule_binding_service.py backend/tests/test_rule_api.py backend/tests/test_project_data_api.py backend/tests/test_phase1_rule_project_data_integration.py -q
~~~

Expected: PASS.

- [ ] Step 4: Run repository-level checks

Run:

~~~powershell
backend/.venv/Scripts/python.exe -m ruff check backend
backend/.venv/Scripts/python.exe -m pytest backend/tests -q
~~~

Expected: Ruff exits 0 and the full backend suite passes. If an unrelated pre-existing test fails, record its exact name and diff scope; do not modify unrelated files to hide it.

- [ ] Step 5: Verify runtime health without enabling later phases

Start the current Windows development runtime using the repository’s documented script from PowerShell after applying the existing execution-policy workaround if needed. Then verify:

~~~powershell
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8000/api/health
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8000/workspace/gallery
~~~

Expected: both endpoints return success; no frontend route or database startup error occurs. Do not claim ONLYOFFICE or Phase 2 UI support from this check.

- [ ] Step 6: Commit Phase 1 verification

~~~powershell
git add backend/tests/test_phase1_rule_project_data_integration.py docs/superpowers/plans/2026-09-04-phase1-rule-library-project-data-foundation.md
git commit -m "test: verify phase 1 rule and project data flow"
~~~

## Traceability to the Approved Specification

| Approved requirement | Plan coverage |
| --- | --- |
| Knowledge and rule libraries are separate | Task 4 stores structured rules and source references; Task 5 does not use vector retrieval for applicability |
| Project data domain is the cross-document source of truth | Task 3 creates current values, revisions, candidates, and conflicts |
| Strong/warning/guidance levels | Task 4 validates levels; Task 5 enforces deterministic blocking limits |
| Project locks exact rule versions and manually migrates | Task 5 binding and migration preview/migrate |
| No last-write-wins | Task 3 expected revision and conflict workflow |
| Existing accounts and project roles | Task 1 role table/backfill and Task 2 authorization |
| Admin manages rule library | Task 4 and Task 6 tenant-admin endpoints |
| All changes are auditable | Task 6 event allow-list and Task 7 integration assertion |
| Existing projects remain usable | Task 1 legacy fallback; no automatic bindings |
| TDD and incremental commits | Every task has failing tests, focused runs, and a commit |
| ONLYOFFICE, document zones, OCR, report generation | Explicitly deferred to later phases; no accidental Phase 1 coupling |

## Phase 1 Acceptance Checklist

- [ ] A legacy project with only AuditCase.owner_user_id and member_user_ids_json remains accessible.
- [ ] Startup backfill is idempotent and never downgrades an explicit role.
- [ ] Tenant administrator can create a rule set, draft a version, validate it, and publish it.
- [ ] Published rule versions cannot be edited in place.
- [ ] Project can bind a published rule version and the binding stores the exact version ID.
- [ ] Replacing an active binding requires migration preview and a reason.
- [ ] Strong model-assisted rules are rejected at validation time.
- [ ] Project member can submit a field candidate with source location and evidence excerpt.
- [ ] Only authorized reviewer/project administrator can approve or reject the candidate.
- [ ] Approval creates revision 1 and an append-only revision record.
- [ ] Stale approval creates a conflict and does not overwrite the approved value.
- [ ] Conflict resolution is explicit and auditable.
- [ ] Deterministic mandatory failures are marked blocking; warning/guidance/model-assisted results are not direct blockers.
- [ ] No rule text, prompt, credential, or full evidence excerpt is stored in audit event metadata.
- [ ] Cross-tenant and cross-project access is rejected.
- [ ] Existing audit-case API tests and full backend tests pass.
- [ ] /api/health and /workspace/gallery remain healthy.

## Known Phase 1 Risks and Controls

| Risk | Control in this plan |
| --- | --- |
| New role rows disagree with legacy member JSON | Explicit-role-first lookup plus idempotent owner/member backfill |
| Published rule is silently changed | Immutable version rows and content hash |
| Field approval overwrites a newer value | Transactional expected-revision check and conflict record |
| AI result becomes a hard rule | Mandatory/model-assisted validation rejection and indeterminate runtime result |
| Rule bindings drift to latest library version | Exact version IDs and explicit migration only |
| Event metadata leaks sensitive text | Existing allow-list extended only with IDs/status/keys/revisions |
| Current dirty worktree is overwritten | Exact-path staging per commit and no frontend changes |
| SQLite assumptions leak into PostgreSQL work | Phase 1 uses existing SQLite-compatible SQLModel patterns and defers PostgreSQL |

## Execution Handoff

After this plan is approved for execution, implement Tasks 1–7 in order. Each task is independently reviewable and must stop at its commit checkpoint if a focused test or migration check fails. Phase 2 begins only after the Phase 1 acceptance checklist is complete.
