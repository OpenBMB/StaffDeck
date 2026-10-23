"""入库链路对模型的依赖边界：模型只是增强项，缺模型/坏模型都不能阻断入库。

背景：``_default_model_config`` 以前直接调用 ``resolve_model_config_for_runtime``，
一个「启用了但没做验证」的默认模型会让整次入库失败，job.error 是原文
``409: MODEL_CONFIG_VERIFICATION_REQUIRED``，于是「上传知识库要先配模型」成了
用户侧的错觉。这组用例守住三条边界：

1. 完全没模型 → 入库成功，按章节结构分桶，检索仍可用；
2. 默认模型不可用 → 降级为无模型，入库仍然成功，只是不做语义分桶与知识发现；
3. 在线编辑保存（``use_llm=False``）根本不解析模型。
"""

from __future__ import annotations

import base64

import pytest
from fastapi import HTTPException
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.db.models import (
    KnowledgeBase,
    KnowledgeBucket,
    KnowledgeChunk,
    KnowledgeDiscoverySuggestion,
    KnowledgeDocument,
    ModelConfig,
    Tenant,
)
from app.knowledge.schema import KnowledgeSearchRequest
from app.knowledge.service import IngestPayload, KnowledgeService
from app.llm.model_config_resolver import _fingerprint

SAMPLE_TEXT = """# 行李寄存

住客可在 L 层免费寄存行李，营业时间 7:00-23:00。

## 收费标准

住客免费，非住客每件 20 元/天。
"""


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _test_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _unverified_default_model(tenant_id: str) -> ModelConfig:
    """默认模型：启用、设为默认，但从未做过连通性验证。

    ``openai_responses`` 不落在 ``_is_implicit_legacy_openai`` 的兼容窗口里，
    因此 ``resolve_model_config_for_runtime`` 会抛
    ``MODEL_CONFIG_VERIFICATION_REQUIRED`` —— 这正是线上把入库打挂的配置形态。
    """

    return ModelConfig(
        tenant_id=tenant_id,
        name="网关模型",
        api_key_encrypted="encrypted",
        model="deepseek-v4-flash",
        api_protocol="openai_responses",
        trust_status="unverified",
        is_default=True,
        enabled=True,
    )


def _prepare(db: Session, tenant_id: str) -> str:
    db.add(Tenant(id=tenant_id, name="Demo"))
    kb = KnowledgeBase(id=f"kb_{tenant_id}", tenant_id=tenant_id, name="默认知识库")
    db.add(kb)
    db.commit()
    return kb.id


def _run_ingest(db: Session, tenant_id: str, knowledge_base_id: str):
    service = KnowledgeService(db)
    job = service.create_ingest_job(
        IngestPayload(
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
            filename="行李寄存.md",
            content_base64=_b64(SAMPLE_TEXT),
            title="行李寄存",
        )
    )
    service._run_ingest_job(job.id)  # noqa: SLF001 - 同步执行后台任务
    db.refresh(job)
    return service, job


def test_ingest_succeeds_without_any_model_config() -> None:
    with _test_session() as db:
        knowledge_base_id = _prepare(db, "tenant_none")
        service, job = _run_ingest(db, "tenant_none", knowledge_base_id)

        assert job.status == "succeeded", job.error
        assert job.error is None
        document = db.get(KnowledgeDocument, job.document_id)
        assert document is not None
        assert document.status == "ready"
        assert document.chunk_count > 0

        buckets = db.exec(
            select(KnowledgeBucket).where(KnowledgeBucket.document_id == document.id)
        ).all()
        assert buckets
        # 无模型时走结构分桶：标题来自一级章节，section_ids 仍完整
        assert all(bucket.metadata_json.get("section_ids") for bucket in buckets)
        assert all(bucket.metadata_json.get("bucket_type") == "structure" for bucket in buckets)

        chunks = db.exec(
            select(KnowledgeChunk).where(KnowledgeChunk.document_id == document.id)
        ).all()
        assert chunks
        assert db.exec(select(KnowledgeDiscoverySuggestion)).all() == []

        # 没有模型也能检索：退回词法 + 结构化路由
        response = service.search(
            KnowledgeSearchRequest(
                tenant_id="tenant_none",
                knowledge_base_ids=[knowledge_base_id],
                query="行李寄存",
                mode="debug",
                need_evidence_pack=True,
            )
        )
        assert response.chunks
        assert response.evidence_pack


def test_ingest_degrades_when_default_model_is_unverified() -> None:
    with _test_session() as db:
        knowledge_base_id = _prepare(db, "tenant_bad")
        db.add(_unverified_default_model("tenant_bad"))
        db.commit()

        _service, job = _run_ingest(db, "tenant_bad", knowledge_base_id)

        # 关键断言：坏模型不能把整次入库打挂
        assert job.status == "succeeded", job.error
        assert job.error is None
        document = db.get(KnowledgeDocument, job.document_id)
        assert document is not None
        assert document.status == "ready"
        assert document.chunk_count > 0

        buckets = db.exec(
            select(KnowledgeBucket).where(KnowledgeBucket.document_id == document.id)
        ).all()
        assert buckets
        assert all(bucket.metadata_json.get("bucket_type") == "structure" for bucket in buckets)
        # 知识发现同样跳过，不产生任何建议
        assert db.exec(select(KnowledgeDiscoverySuggestion)).all() == []


def test_edit_save_does_not_resolve_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """在线编辑保存走 use_llm=False，不应该为了补桶去解析模型。"""

    with _test_session() as db:
        knowledge_base_id = _prepare(db, "tenant_edit")
        service, job = _run_ingest(db, "tenant_edit", knowledge_base_id)
        document = db.get(KnowledgeDocument, job.document_id)
        assert document is not None and document.status == "ready"

        def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
            raise AssertionError("use_llm=False 时不应解析模型")

        monkeypatch.setattr(KnowledgeService, "_default_model_config", _boom)

        updated = service.replace_document_content(
            document, SAMPLE_TEXT + "\n补充：23:00 后请联系前台。"
        )
        assert updated.status == "ready"
        buckets = db.exec(
            select(KnowledgeBucket).where(KnowledgeBucket.document_id == document.id)
        ).all()
        assert buckets
        assert "23:00 后请联系前台" in updated.metadata_json["raw_text"]


def test_default_model_resolution_degrades_but_explicit_choice_reports_readable_error() -> None:
    from app.api.knowledge import _get_default_model, _get_request_model

    with _test_session() as db:
        bad_default = _unverified_default_model("tenant_api")
        other = ModelConfig(
            tenant_id="tenant_api",
            name="另一个没验证的模型",
            api_key_encrypted="encrypted",
            model="glm-5.2",
            api_protocol="openai_responses",
            trust_status="unverified",
            is_default=False,
            enabled=True,
        )
        db.add(bad_default)
        db.add(other)
        db.commit()

        # 默认模型是平台替用户选的 → 不可用时按「没有模型」降级
        assert _get_default_model(db, "tenant_api") is None
        # 前端下拉默认选中「默认模型」，等价于没选，也应降级
        assert _get_request_model(db, "tenant_api", bad_default.id) is None

        # 用户显式挑了另一个模型 → 不能悄悄忽略，给出可操作的中文提示
        with pytest.raises(HTTPException) as excinfo:
            _get_request_model(db, "tenant_api", other.id)
        assert excinfo.value.status_code == 409
        assert "模型" in str(excinfo.value.detail)
        assert "MODEL_CONFIG" not in str(excinfo.value.detail)


def test_verified_default_model_is_still_resolved() -> None:
    """降级不能把正常路径一起砍掉：验证通过的模型仍要被解析出来。"""

    with _test_session() as db:
        model = ModelConfig(
            tenant_id="tenant_ok",
            name="正常模型",
            api_key_encrypted="encrypted",
            model="deepseek-v4-flash",
            api_protocol="openai_chat_completions",
            trust_status="verified",
            is_default=True,
            enabled=True,
        )
        db.add(model)
        db.commit()
        db.refresh(model)
        model.verified_fingerprint = _fingerprint(model)
        db.add(model)
        db.commit()

        resolved = KnowledgeService(db)._default_model_config("tenant_ok")  # noqa: SLF001
        assert resolved is not None
        assert resolved.model == "deepseek-v4-flash"


def test_employee_scoped_upload_needs_no_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """在某个员工下上传知识库：接口层不解析模型，没有模型也能建出入库任务。"""

    from app.api import knowledge as knowledge_api
    from app.db.models import AgentProfile, User
    from app.knowledge.schema import KnowledgeDocumentUploadRequest

    scheduled: list[str] = []
    monkeypatch.setattr(
        knowledge_api,
        "schedule_knowledge_ingest",
        lambda job_id, **kwargs: scheduled.append(job_id),
    )

    with _test_session() as db:
        db.add(Tenant(id="tenant_emp", name="Demo"))
        db.add(AgentProfile(id="agent_1", tenant_id="tenant_emp", name="前台助理"))
        user = User(
            id="user_owner",
            tenant_id="tenant_emp",
            username="owner",
            role="admin",
            password_hash="unused",
        )
        db.add(user)
        db.commit()

        job = knowledge_api.upload_document(
            KnowledgeDocumentUploadRequest(
                tenant_id="tenant_emp",
                filename="行李寄存.md",
                content_base64=_b64(SAMPLE_TEXT),
                title="行李寄存",
            ),
            agent_id="agent_1",
            db=db,
            current_user=user,
        )

        assert job.status == "queued"
        assert job.knowledge_base_id
        assert scheduled == [job.id]
        # 没有模型配置也不该被拦下来
        assert db.exec(select(ModelConfig)).all() == []
