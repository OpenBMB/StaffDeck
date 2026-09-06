from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, create_engine, select

from app.db.models import (
    ProjectDataConflict,
    ProjectDataFieldDefinition,
    ProjectDataValue,
    RuleDefinition,
    RuleSetVersion,
)
from app.project_data.schema import (
    ProjectDataCandidateCreate,
    ProjectDataCandidateRead,
    ProjectDataConflictRead,
    ProjectDataValueRead,
    SourceRef,
)


def test_phase1_json_payloads_round_trip(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'phase1-json.db'}")
    SQLModel.metadata.create_all(engine)

    rule = RuleDefinition(
        id="rule-1",
        tenant_id="tenant_demo",
        rule_set_version_id="ruleset-version-1",
        rule_key="organization.name.required",
        name="组织名称必填",
        workflow_nodes_json=["project_data.approval"],
        information_domains_json=["organization"],
        document_types_json=["audit_report"],
        field_keys_json=["organization.legal_name"],
        condition_json={"operator": "not_empty"},
        input_requirements_json=[{"field_key": "organization.legal_name"}],
        evidence_requirements_json=[{"kind": "material"}],
        source_refs_json=[{"document_id": "standard-1", "location": "4.1"}],
    )
    conflict = ProjectDataConflict(
        id="conflict-1",
        tenant_id="tenant_demo",
        audit_case_id="case-1",
        field_key="organization.legal_name",
        current_revision=1,
        candidate_ids_json=["candidate-1", "candidate-2"],
    )

    with Session(engine) as db:
        db.add(rule)
        db.add(conflict)
        db.commit()

    with Session(engine) as db:
        stored_rule = db.exec(select(RuleDefinition)).one()
        stored_conflict = db.exec(select(ProjectDataConflict)).one()
        assert stored_rule.condition_json == {"operator": "not_empty"}
        assert stored_rule.field_keys_json == ["organization.legal_name"]
        assert stored_rule.source_refs_json == [
            {"document_id": "standard-1", "location": "4.1"}
        ]
        assert stored_conflict.candidate_ids_json == ["candidate-1", "candidate-2"]


def test_project_field_has_one_current_row_per_case_and_key(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'phase1-models.db'}")
    SQLModel.metadata.create_all(engine)

    with Session(engine) as db:
        db.add(
            ProjectDataValue(
                id="value-1",
                tenant_id="tenant_demo",
                audit_case_id="case-1",
                field_key="organization.legal_name",
                value_json="甲公司",
                status="approved",
                revision=1,
            )
        )
        db.commit()
        db.add(
            ProjectDataValue(
                id="value-2",
                tenant_id="tenant_demo",
                audit_case_id="case-1",
                field_key="organization.legal_name",
                value_json="乙公司",
                status="approved",
                revision=1,
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()


def test_system_field_definition_key_is_unique_in_sqlite(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'system-field-definitions.db'}")
    SQLModel.metadata.create_all(engine)

    with Session(engine) as db:
        db.add(
            ProjectDataFieldDefinition(
                id="field-definition-1",
                tenant_id=None,
                field_key="organization.legal_name",
                label="法定名称",
                value_type="string",
                information_domain="organization",
                scope="organization",
            )
        )
        db.commit()
        db.add(
            ProjectDataFieldDefinition(
                id="field-definition-2",
                tenant_id=None,
                field_key="organization.legal_name",
                label="企业名称",
                value_type="string",
                information_domain="organization",
                scope="organization",
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()


def test_project_field_definition_source_optional_round_trips(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'source-optional.db'}")
    SQLModel.metadata.create_all(engine)

    with Session(engine) as db:
        db.add(
            ProjectDataFieldDefinition(
                id="field-definition-source-optional",
                tenant_id="tenant_demo",
                field_key="tenant.source_optional",
                label="可无来源字段",
                value_type="text",
                information_domain="tenant",
                scope="project",
                source_optional=True,
            )
        )
        db.commit()

    with Session(engine) as db:
        stored = db.get(ProjectDataFieldDefinition, "field-definition-source-optional")
        assert stored is not None
        assert stored.source_optional is True


def test_open_conflict_is_unique_per_candidate_and_target_revision(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'conflict-idempotency.db'}")
    SQLModel.metadata.create_all(engine)

    with Session(engine) as db:
        db.add(
            ProjectDataConflict(
                id="conflict-1",
                tenant_id="tenant_demo",
                audit_case_id="case-1",
                field_key="organization.legal_name",
                current_revision=1,
                trigger_candidate_id="candidate-1",
                candidate_ids_json=["candidate-1"],
            )
        )
        db.commit()
        db.add(
            ProjectDataConflict(
                id="conflict-2",
                tenant_id="tenant_demo",
                audit_case_id="case-1",
                field_key="organization.legal_name",
                current_revision=1,
                trigger_candidate_id="candidate-1",
                candidate_ids_json=["candidate-1"],
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()


def test_project_data_schemas_serialize_nested_sources_and_revision_fields() -> None:
    source = SourceRef(
        material_id="material-1",
        material_version_id="material-version-1",
        document_id="document-1",
        location="page:2",
        evidence_excerpt="甲公司",
    )
    request = ProjectDataCandidateCreate(
        field_key="organization.legal_name",
        value="甲公司",
        source=source,
        expected_revision=3,
        note="已核对",
    )
    value = ProjectDataValueRead(
        id="value-1",
        field_key=request.field_key,
        value=request.value,
        status="approved",
        revision=4,
        source=source,
        updated_by_user_id="reviewer-1",
    )
    candidate = ProjectDataCandidateRead(
        id="candidate-1",
        field_key=request.field_key,
        value=request.value,
        source=source,
        status="pending",
        expected_revision=request.expected_revision,
        submitted_by_user_id="editor-1",
    )
    conflict = ProjectDataConflictRead(
        id="conflict-1",
        field_key=request.field_key,
        status="open",
        current_revision=4,
        candidate_ids=[candidate.id, "candidate-2"],
    )

    assert request.model_dump(mode="json") == {
        "field_key": "organization.legal_name",
        "value": "甲公司",
        "source": {
            "material_id": "material-1",
            "material_version_id": "material-version-1",
            "document_id": "document-1",
            "location": "page:2",
            "evidence_excerpt": "甲公司",
        },
        "expected_revision": 3,
        "note": "已核对",
    }
    assert value.model_dump(mode="json")["revision"] == 4
    assert candidate.model_dump(mode="json")["expected_revision"] == 3
    assert conflict.model_dump(mode="json")["candidate_ids"] == [
        "candidate-1",
        "candidate-2",
    ]


def test_published_rule_version_publication_fields_are_persisted(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'published-rule-version.db'}")
    SQLModel.metadata.create_all(engine)
    version = RuleSetVersion(
        id="ruleset-version-1",
        tenant_id="tenant_demo",
        rule_set_id="ruleset-1",
        version=1,
        status="published",
        content_sha256="a" * 64,
    )

    with Session(engine) as db:
        db.add(version)
        db.commit()

    with Session(engine) as db:
        stored = db.get(RuleSetVersion, "ruleset-version-1")
        assert stored is not None
        assert stored.status == "published"
        assert stored.content_sha256 == "a" * 64
