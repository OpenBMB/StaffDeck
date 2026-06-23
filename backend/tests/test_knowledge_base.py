from __future__ import annotations

import base64

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.api.knowledge_bases import knowledge_base_read
from app.db.models import (
    KnowledgeBase,
    KnowledgeBaseVersion,
    KnowledgeBucket,
    KnowledgeChunk,
    KnowledgeDiscoverySuggestion,
    KnowledgeDocument,
    Skill,
    Tenant,
    Tool,
)
from app.knowledge.schema import KnowledgeSearchRequest
from app.knowledge.service import IngestPayload, KnowledgeService
from app.skills.skill_schema import SkillCard


def test_skill_card_rejects_legacy_steps_and_accepts_graph() -> None:
    with pytest.raises(Exception):
        SkillCard(
            skill_id="skill_test",
            name="测试技能",
            steps=[
                {
                    "step_id": "collect",
                    "name": "收集信息",
                    "instruction": "收集用户信息",
                    "expected_user_info": ["name"],
                    "allowed_actions": ["ask_user", "continue_flow"],
                }
            ],
        )

    card = SkillCard(
        skill_id="skill_test",
        name="测试技能",
        nodes=[
            {
                "node_id": "collect",
                "type": "collect_info",
                "name": "收集信息",
                "instruction": "收集用户信息",
                "expected_user_info": ["name"],
                "allowed_actions": ["ask_user", "continue_flow"],
            },
            {
                "node_id": "reply",
                "type": "response",
                "name": "回复",
                "instruction": "回复用户",
                "allowed_actions": ["answer_user"],
            },
        ],
        edges=[{"source_node_id": "collect", "next_node_id": "reply"}],
        start_node_id="collect",
        terminal_node_ids=["reply"],
    )

    assert card.start_node_id == "collect"
    assert card.terminal_node_ids == ["reply"]
    assert [node.node_id for node in card.nodes] == ["collect", "reply"]
    assert card.edges[0].source_node_id == "collect"
    assert card.edges[0].next_node_id == "reply"


def test_knowledge_ingest_creates_document_buckets_and_chunks_without_auto_discovery() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(KnowledgeBase(id="kb_demo", tenant_id="tenant_demo", name="默认知识库"))
        db.commit()
        service = KnowledgeService(db)
        job = service.create_ingest_job(
            IngestPayload(
                tenant_id="tenant_demo",
                knowledge_base_id="kb_demo",
                filename="policy.md",
                content_base64=_b64("# 售后政策\n用户可查询订单。\n\n# 配送\n根据地址评估配送。"),
            )
        )

        service._run_ingest_job(job.id)  # noqa: SLF001 - exercise persistent job logic synchronously.

        job = db.get(type(job), job.id)
        assert job is not None
        assert job.status == "succeeded"
        assert job.document_id
        document = db.get(KnowledgeDocument, job.document_id)
        assert document is not None
        assert document.metadata_json["document_card"]["title"]
        assert document.metadata_json["section_tree"]
        assert document.metadata_json["chunk_stats"]["total_chunks"] > 0
        assert document.metadata_json["bucket_quality"]
        buckets = db.exec(select(KnowledgeBucket).where(KnowledgeBucket.document_id == job.document_id)).all()
        assert buckets
        assert all(bucket.metadata_json.get("section_ids") for bucket in buckets)
        chunks = db.exec(select(KnowledgeChunk).where(KnowledgeChunk.document_id == job.document_id)).all()
        assert chunks
        assert all(chunk.metadata_json.get("section_path") for chunk in chunks)
        response = service.search(
            KnowledgeSearchRequest(
                tenant_id="tenant_demo",
                knowledge_base_ids=["kb_demo"],
                query="配送怎么处理",
                mode="debug",
                need_evidence_pack=True,
            )
        )
        phases = [item["phase"] for item in response.route_trace]
        assert "document_route" in phases
        assert "bucket_route" in phases
        assert "section_expand" in phases
        assert "evidence_pack" in phases
        assert response.selected_documents
        assert response.expanded_sections
        assert response.evidence_pack
        assert response.evidence_pack[0]["source_path"]
        assert response.evidence_pack[0]["excerpt"]
        assert response.chunks
        assert db.exec(select(KnowledgeDiscoverySuggestion)).all() == []


def test_knowledge_search_preserves_fallback_bucket_rank_order() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(KnowledgeBase(id="kb_demo", tenant_id="tenant_demo", name="默认知识库"))
        document = KnowledgeDocument(
            id="kdoc_frontend",
            tenant_id="tenant_demo",
            knowledge_base_id="kb_demo",
            filename="frontend.md",
            file_type="md",
            title="前端规范资料",
            status="ready",
            bucket_count=2,
            chunk_count=2,
            metadata_json={
                "document_card": {
                    "title": "前端规范资料",
                    "summary": "前端编码规范、Vue 3、组件规范和命名规范。",
                }
            },
        )
        irrelevant = KnowledgeBucket(
            id="kbucket_citation",
            tenant_id="tenant_demo",
            knowledge_base_id="kb_demo",
            document_id=document.id,
            bucket_key="citation",
            title="知识引用测试说明",
            summary="回答引用展示规则。",
        )
        frontend = KnowledgeBucket(
            id="kbucket_frontend",
            tenant_id="tenant_demo",
            knowledge_base_id="kb_demo",
            document_id=document.id,
            bucket_key="frontend",
            title="前端编码规范",
            summary="Vue 3、Vite、TypeScript、组件编写和命名规范。",
        )
        db.add(document)
        db.add(irrelevant)
        db.add(frontend)
        db.add(
            KnowledgeChunk(
                tenant_id="tenant_demo",
                knowledge_base_id="kb_demo",
                document_id=document.id,
                bucket_id=irrelevant.id,
                chunk_index=0,
                content="知识引用展示规则。",
                summary="知识引用展示规则。",
                source_ref="citation.md",
            )
        )
        db.add(
            KnowledgeChunk(
                tenant_id="tenant_demo",
                knowledge_base_id="kb_demo",
                document_id=document.id,
                bucket_id=frontend.id,
                chunk_index=0,
                content="前端规范包括 Vue 3、Vite、TypeScript 和组件编写规范。",
                summary="前端规范包括 Vue 3、Vite、TypeScript 和组件编写规范。",
                source_ref="frontend.md",
            )
        )
        db.commit()

        response = KnowledgeService(db).search(
            KnowledgeSearchRequest(
                tenant_id="tenant_demo",
                knowledge_base_ids=["kb_demo"],
                query="前端规范有哪些？",
                mode="chat",
                max_buckets=2,
                need_evidence_pack=True,
            )
        )

        assert [bucket.id for bucket in response.selected_buckets][:2] == ["kbucket_frontend", "kbucket_citation"]


def test_knowledge_base_read_keeps_archived_rows_visible_despite_active_versions() -> None:
    row = KnowledgeBase(id="kb_demo", tenant_id="tenant_demo", name="默认知识库", status="archived")
    version = KnowledgeBaseVersion(
        tenant_id="tenant_demo",
        knowledge_base_id=row.id,
        version="1.0.0",
        name=row.name,
        status="active",
    )

    overall_read = knowledge_base_read(row, {}, version_row=version)
    branch_read = knowledge_base_read(
        row,
        {},
        version_row=version,
        branch_meta={"status": "inactive", "base_version": "1.0.0", "head_version": "1.0.0", "sync_state": "synced"},
    )

    assert overall_read.status == "archived"
    assert branch_read.status == "archived"


def test_confirm_discovery_is_required_before_tool_or_skill_enters_runtime() -> None:
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(KnowledgeBase(id="kb_demo", tenant_id="tenant_demo", name="默认知识库"))
        suggestion = KnowledgeDiscoverySuggestion(
            tenant_id="tenant_demo",
            knowledge_base_id="kb_demo",
            document_id="doc_1",
            suggestion_type="tool",
            title="会员权益核对",
            payload_json={
                "name": "member.benefit_reconcile",
                "display_name": "会员权益核对",
                "method": "POST",
                "url": "/api/mock/member/benefit-reconcile",
            },
        )
        db.add(suggestion)
        db.commit()
        db.refresh(suggestion)

        assert db.exec(select(Tool)).all() == []
        result = KnowledgeService(db).confirm_discovery(suggestion)

        assert result["status"] == "created"
        assert db.exec(select(Tool).where(Tool.name == "member.benefit_reconcile")).first()
        assert db.exec(select(Skill)).all() == []


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _test_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)
