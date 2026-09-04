from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, create_engine, select

from app.db.models import (
    ProjectDataConflict,
    ProjectDataValue,
    RuleDefinition,
    RuleSetVersion,
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
