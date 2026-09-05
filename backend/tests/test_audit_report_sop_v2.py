from __future__ import annotations

from sqlmodel import Session, SQLModel, create_engine, select

from app.db.models import HarnessTaskFrameRecord, Skill, SkillVersion
from app.db.seed import load_audit_report_sop_v2, seed_audit_report_sop_v2


def test_audit_report_sop_v2_is_project_driven() -> None:
    card = load_audit_report_sop_v2()

    assert card.version == "2.0.0"
    assert card.required_info == [
        "audit_case_id",
        "report_type",
        "management_systems",
        "organization_name",
        "material_manifest_status",
        "evidence_coverage_status",
        "knowledge_coverage_status",
    ]
    assert [node.node_id for node in card.nodes] == [
        "select_or_create_audit_case",
        "collect_materials",
        "ingest_and_validate_materials",
        "build_material_evidence_ledger",
        "retrieve_reference_knowledge",
        "trim_template",
        "generate_report_sections",
        "consistency_and_coverage_check",
        "output_and_confirm",
        "handoff_lead_auditor",
    ]
    collect = next(node for node in card.nodes if node.node_id == "collect_materials")
    assert "只询问缺失、失败或版本冲突的材料" in collect.instruction
    assert collect.expected_user_info == ["material_manifest_status"]
    assert collect.capability_refs.general_skill_ids == []
    assert collect.capability_refs.tool_ids == []
    assert collect.capability_refs.knowledge_base_ids == []


def test_audit_report_sop_requires_structured_pipeline_capabilities() -> None:
    card = load_audit_report_sop_v2()
    by_id = {node.node_id: node for node in card.nodes}

    assert "audit_case_manifest" in by_id["collect_materials"].allowed_actions
    assert "audit_evidence_process" in by_id["build_material_evidence_ledger"].allowed_actions
    assert "audit_report_generate" in by_id["generate_report_sections"].allowed_actions
    assert "audit_report_status" in by_id["consistency_and_coverage_check"].allowed_actions
    assert by_id["retrieve_reference_knowledge"].capability_refs.required_knowledge_base_ids


def test_seed_audit_report_sop_v2_is_idempotent_and_preserves_old_version(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'sop-seed.db'}")
    SQLModel.metadata.create_all(engine)

    old_content = {
        "skill_id": "audit_report_generation_sop",
        "name": "旧版审核报告生成 SOP",
        "version": "1.0.0",
        "nodes": [],
    }
    with Session(engine) as db:
        db.add(
            Skill(
                tenant_id="tenant_demo",
                skill_id="audit_report_generation_sop",
                version="1.0.0",
                name="旧版审核报告生成 SOP",
                description="旧版",
                content_json=old_content,
                status="published",
            )
        )
        db.commit()

        seed_audit_report_sop_v2(db)
        seed_audit_report_sop_v2(db)
        db.commit()

        versions = db.exec(
            select(SkillVersion)
            .where(
                SkillVersion.tenant_id == "tenant_demo",
                SkillVersion.skill_id == "audit_report_generation_sop",
            )
            .order_by(SkillVersion.version)
        ).all()
        assert [version.version for version in versions] == ["1.0.0", "2.0.0"]
        assert versions[0].content_json == old_content

        current = db.exec(
            select(Skill).where(
                Skill.tenant_id == "tenant_demo",
                Skill.skill_id == "audit_report_generation_sop",
            )
        ).one()
        assert current.version == "2.0.0"
        assert current.content_json["version"] == "2.0.0"


def test_seed_audit_report_sop_v2_does_not_switch_running_old_task(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'sop-running.db'}")
    SQLModel.metadata.create_all(engine)
    old_content = {
        "skill_id": "audit_report_generation_sop",
        "name": "旧版审核报告生成 SOP",
        "version": "1.0.0",
        "nodes": [],
    }

    with Session(engine) as db:
        db.add(
            Skill(
                tenant_id="tenant_demo",
                skill_id="audit_report_generation_sop",
                version="1.0.0",
                name="旧版审核报告生成 SOP",
                content_json=old_content,
                status="published",
            )
        )
        db.add(
            HarnessTaskFrameRecord(
                tenant_id="tenant_demo",
                session_id="session-1",
                source_turn_id="turn-1",
                task_id="task-1",
                skill_id="audit_report_generation_sop",
                status="running",
            )
        )
        db.commit()

        seed_audit_report_sop_v2(db)
        db.commit()

        current = db.exec(
            select(Skill).where(
                Skill.tenant_id == "tenant_demo",
                Skill.skill_id == "audit_report_generation_sop",
            )
        ).one()
        versions = db.exec(
            select(SkillVersion).where(
                SkillVersion.tenant_id == "tenant_demo",
                SkillVersion.skill_id == "audit_report_generation_sop",
            )
        ).all()
        assert current.version == "1.0.0"
        assert current.content_json == old_content
        assert [version.version for version in versions] == ["2.0.0"]
