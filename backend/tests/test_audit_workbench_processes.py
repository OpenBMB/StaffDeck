from __future__ import annotations

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.audit_cases.workbench_processes import (
    all_process_definitions,
    evaluate_process_gate,
    get_process_definition,
)
from app.db.models import AuditCase, AuditCaseDocument, AuditCaseDocumentVersion, AuditWorkItem
from app.db.workbench_checks import AuditDocumentCheck


@pytest.fixture
def process_db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'process-gates.db'}", connect_args={"check_same_thread": False}
    )
    SQLModel.metadata.create_all(engine)
    case = AuditCase(
        id="case",
        tenant_id="tenant",
        owner_user_id="owner",
        organization_name="测试企业",
        report_type="再认证",
    )
    with Session(engine) as db:
        db.add(case)
        for document_id in ["plan", "field"]:
            version = AuditCaseDocumentVersion(
                id=f"{document_id}-v1",
                tenant_id="tenant",
                audit_case_id="case",
                document_id=document_id,
                version=1,
                content_format="markdown",
                content=f"内容 {document_id}",
                content_sha256="sha",
                created_by_user_id="owner",
            )
            db.add(
                AuditCaseDocument(
                    id=document_id,
                    tenant_id="tenant",
                    audit_case_id="case",
                    document_key=document_id,
                    title=document_id,
                    document_type="work_document",
                    zone="workspace",
                    active_version_id=version.id,
                    created_by_user_id="owner",
                    updated_by_user_id="owner",
                )
            )
            db.add(version)
        db.commit()
        yield db, case


def approved_item(process_number: int, document_id: str) -> AuditWorkItem:
    return AuditWorkItem(
        id=f"item-{process_number}",
        tenant_id="tenant",
        audit_case_id="case",
        document_id=document_id,
        document_version_id=f"{document_id}-v1",
        process_number=process_number,
        title=document_id,
        status="approved",
        revision=1,
        assigned_to_user_id="owner",
        reviewer_user_id="reviewer",
        submitted_by_user_id="owner",
        approved_version_id=f"{document_id}-v1",
    )


def test_registry_exposes_phase_5b_process_definitions():
    assert get_process_definition(17).predecessor_numbers == (16,)
    assert get_process_definition(21).requires_fresh_check is True
    assert get_process_definition(23).predecessor_numbers == (18,)
    assert get_process_definition(23).required_reference_process_numbers == (18,)

    enabled = {row.number for row in all_process_definitions() if row.enabled}
    assert {15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26} <= enabled
    assert 28 not in enabled


def test_process_gate_requires_approved_predecessors_and_ignores_optional_process_19(process_db):
    db, case = process_db
    with pytest.raises(KeyError):
        get_process_definition(37)

    blocked = evaluate_process_gate(db, case, 17, "plan")
    assert blocked["ready"] is False
    assert {blocker["code"] for blocker in blocked["blockers"]} == {"PROCESS_PRECONDITION_REQUIRED"}

    db.add(approved_item(16, "field"))
    db.add(approved_item(18, "field"))
    db.commit()

    ready_17 = evaluate_process_gate(db, case, 17, "plan")
    assert ready_17["ready"] is True
    assert ready_17["required_reference_document_ids"] == ["field"]

    ready_23 = evaluate_process_gate(db, case, 23, "plan")
    assert ready_23["ready"] is True
    assert not any(blocker["process_number"] == 19 for blocker in ready_23["blockers"])


def test_process_21_requires_a_fresh_error_free_document_check(process_db):
    db, case = process_db
    db.add(approved_item(17, "plan"))
    db.commit()

    gate = evaluate_process_gate(db, case, 21, "field")
    assert gate["ready"] is False
    assert {blocker["code"] for blocker in gate["blockers"]} == {
        "CHECK_REQUIRED",
    }

    db.add(
        AuditDocumentCheck(
            id="check-1",
            tenant_id="tenant",
            audit_case_id="case",
            document_id="field",
            document_version_id="field-v1",
            created_by_user_id="owner",
            request_key="check-1",
            request_hash="hash",
            status="completed",
            findings_json=[{"severity": "warning"}],
        )
    )
    db.commit()
    ready = evaluate_process_gate(db, case, 21, "field")
    assert ready["ready"] is True
    assert ready["check"]["status"] == "completed"
